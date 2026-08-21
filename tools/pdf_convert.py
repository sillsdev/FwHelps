"""Convert the FwHelps PDFs into markdown.

Thirteen PDFs, 349 pages: technical notes, utility documentation, and the
Conceptual Introduction. All have real text layers, so no OCR is involved.

Heading structure comes from one of two places:

  * 7 PDFs carry bookmarks, which give exact headings and levels.
  * 6 -- the Word-produced "Technical Notes" family -- carry none, so headings
    are inferred from font size and weight.

Inference is pinned. The expected outline of every PDF is recorded in
pdf_outlines.json and re-checked on each build: these files change roughly once
a decade (9 of 13 have exactly one commit in the repo's history), so drift
almost always means the inference broke rather than the document changing.
A mismatch fails the build instead of quietly shipping a mis-structured corpus.

Usage:
    python tools/pdf_convert.py --repo . --out export [--update-outlines]
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

import pymupdf as fitz  # "fitz" name is deprecated; alias keeps call sites short
import pymupdf4llm
from pymupdf4llm.helpers.pymupdf_rag import TocHeaders

OUTLINES = Path(__file__).parent / "pdf_outlines.json"


def slug_path(rel: str) -> str:
    """Space-free output path.

    pymupdf4llm rewrites spaces to underscores when it derives image filenames
    from image_path, so a directory created as "Technical Notes_images" is not
    the one it writes into. Underscores throughout also keep the URLs clean and
    match the CHM side of the export, which RoboHelp already names that way.
    """
    return "/".join(part.replace(" ", "_") for part in rel.split("/"))


def clean(text: str) -> str:
    """Normalise the non-breaking spaces Word leaves in headings and bookmarks."""
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", text).strip()


class FontHeaders:
    """Infer heading levels from font size and weight.

    The corpus's bookmark-less PDFs are Word documents with a consistent style:
    12pt regular body, 18pt bold H1, 14pt bold H2. Crucially, 12pt *bold* is
    used for inline emphasis ("Note:", "Tip:") and must not become a heading --
    so size alone is not enough, and bold alone is not enough either. A span has
    to be both bold and meaningfully larger than the body text.
    """

    BOLD = 1 << 4

    def __init__(self, doc: fitz.Document, min_lines: int = 2):
        sizes: collections.Counter = collections.Counter()
        bold_sizes: collections.Counter = collections.Counter()

        for page in doc:
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    spans = line.get("spans", [])
                    if not spans:
                        continue
                    text = "".join(s["text"] for s in spans).strip()
                    if not text:
                        continue
                    span = spans[0]
                    size = round(span["size"], 1)
                    sizes[size] += 1
                    if span["flags"] & self.BOLD:
                        bold_sizes[size] += 1

        self.body = sizes.most_common(1)[0][0] if sizes else 12.0
        # Candidate heading sizes: bold, bigger than the body, and used often
        # enough to be a real style rather than a one-off.
        candidates = sorted(
            (s for s, n in bold_sizes.items() if s > self.body + 0.4 and n >= min_lines),
            reverse=True,
        )
        self.levels = {size: i + 1 for i, size in enumerate(candidates[:6])}

    def get_header_id(self, span: dict, page=None) -> str:
        if not (span["flags"] & self.BOLD):
            return ""
        level = self.levels.get(round(span["size"], 1))
        return "#" * level + " " if level else ""


def running_margins(doc: fitz.Document, threshold: float = 0.6) -> tuple[float, float]:
    """Find the top/bottom bands occupied by running headers and footers.

    Every Word-produced PDF here repeats a header ("2 Getting started ... 4")
    and footer ("Technical Notes on ...doc  Edited on 8/13/2026") on each page;
    left in, they appear in the markdown once per page.

    Measured per document, not globally: silewp2007_002.pdf carries real body
    text where the others put a footer, so a blanket margin would silently eat
    content. A band only counts as furniture if it recurs on most pages.
    """
    if not doc.page_count:
        return 0.0, 0.0
    height = doc[0].rect.height
    top_ys: list[float] = []
    bottom_ys: list[float] = []
    top_pages: set[int] = set()
    bottom_pages: set[int] = set()

    for i, page in enumerate(doc):
        for block in page.get_text("dict")["blocks"]:
            y0, y1 = block["bbox"][1], block["bbox"][3]
            text = "".join(
                s["text"] for line in block.get("lines", []) for s in line["spans"]
            ).strip()
            if not text:
                continue
            if y1 < height * 0.12:
                top_pages.add(i)
                top_ys.append(y1)
            elif y0 > height * 0.90:
                bottom_pages.add(i)
                bottom_ys.append(y0)

    def pct(values: list[float], q: float) -> float:
        vs = sorted(values)
        return vs[min(len(vs) - 1, int(q * len(vs)))]

    need = threshold * doc.page_count
    top = pct(top_ys, 0.9) + 2 if len(top_pages) >= need else 0.0
    bottom = height - pct(bottom_ys, 0.1) + 2 if len(bottom_pages) >= need else 0.0
    return round(top, 1), round(bottom, 1)


LEADER = re.compile(r"\.{4,}\s*\d+\s*$")
INDEX_HEAD = re.compile(r"^#{1,6}\s*\**\s*(language|subject|topic)?\s*index\s*\**\s*$", re.I)
HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def strip_toc(md: str) -> str:
    """Drop the document's own table of contents.

    Every Word-produced PDF opens with dotted-leader contents lines, which
    pymupdf4llm renders as a bogus one-column table -- 16 to 37 lines of
    "2.1 Starting up a Project .......... 4" per document. The markdown file
    already has real headings, and this branch has a README index, so the
    inline copy is pure noise for a reader and for retrieval alike.
    """
    out = []
    for line in md.splitlines():
        bare = line.strip().strip("|").strip()
        if LEADER.search(bare):
            continue
        # The table skeleton left behind once its rows are gone. Tested with a
        # character-set check, not a regex: a nested-quantifier pattern like
        # (:?-+:?\s*\|?)+ backtracks exponentially on a long separator row.
        # A separator is only real if an actual table row precedes it.
        if bare and set(bare) <= set("|-: "):
            prev = next((x for x in reversed(out) if x.strip()), "")
            if not prev.strip().startswith("|"):
                continue
        if re.fullmatch(r"\|\s*Contents\s*\|", line.strip(), re.I):
            continue
        out.append(line)
    return "\n".join(out)


def strip_back_index(md: str) -> str:
    """Drop a back-of-book index.

    ConceptualIntroFLEx ends with "Language index" and "Subject index": several
    thousand words of headword-plus-page-number that carry no sentences, cannot
    be followed without the printed pagination, and would otherwise be the
    single largest retrievable block in the document.

    Only honoured near the end of a document, so a section legitimately called
    "Index" mid-text is left alone.
    """
    lines = md.splitlines()
    for i, line in enumerate(lines):
        if INDEX_HEAD.match(line.strip()) and i > len(lines) * 0.75:
            return "\n".join(lines[:i]).rstrip() + "\n"
    return md


def demote_headings(md: str) -> str:
    """Push every heading down one level and strip emphasis markers.

    The page's own H1 is the document title, added by the caller, so the PDF's
    top-level sections belong at H2. Word also bolds its headings, which pandoc
    faithfully reproduces as "# **1 Introduction**".
    """
    out = []
    for line in md.splitlines():
        m = HEADING.match(line)
        if not m:
            out.append(line)
            continue
        level = min(6, len(m.group(1)) + 1)
        text = re.sub(r"^[*_]{1,2}|[*_]{1,2}$", "", m.group(2).strip()).strip()
        out.append(f"{'#' * level} {text}" if text else "")
    return "\n".join(out)


def drop_repeated_title(md: str, title: str) -> str:
    """Remove the document's own title heading when it restates the page title.

    Otherwise every PDF opens with the title twice -- once as the H1 this tool
    adds, once as the heading from the PDF's title page ("Technical Notes on
    FieldWorks Send-Receive" then "Technical Notes on Fieldworks Send/Receive").
    Compared on letters and digits alone, so punctuation and casing differences
    like Send-Receive vs Send/Receive still count as the same title.
    """
    key = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    want = key(title)
    lines = md.splitlines()
    for i, line in enumerate(lines[:12]):
        m = HEADING.match(line)
        if m and key(m.group(2)) == want:
            del lines[i]
            break
    return "\n".join(lines).strip() + "\n"


def outline_of(md: str) -> list[tuple[int, str]]:
    """Headings in generated markdown, ignoring anything inside fenced code."""
    out, fenced = [], False
    for line in md.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = re.match(r"^(#{1,6})\s+(.*\S)\s*$", line)
        if m:
            out.append((len(m.group(1)), clean(m.group(2))))
    return out


DIGITS = re.compile(r"\d+")
HTML_TABLE = re.compile(r"<table\b.*?</table>", re.S | re.I)


def strip_furniture(pages: list[str], threshold: float = 0.6) -> list[str]:
    """Remove running headers and footers from per-page markdown.

    Detection is by repetition rather than by position, which keeps it
    independent of the producing toolchain -- the corpus spans four Word
    versions, XLingPaper/LaTeX, and two Acrobat Distiller variants. A line
    qualifies only if it sits at the very top or bottom of its page and recurs,
    modulo page numbers, on most pages.
    """
    if len(pages) < 3:
        return pages

    counts: collections.Counter = collections.Counter()
    for page in pages:
        lines = [ln.strip() for ln in page.splitlines() if ln.strip()]
        for line in set(lines[:2] + lines[-2:]):
            counts[DIGITS.sub("#", line)] += 1

    # Word repeats the *current section* heading in the running header, so any
    # one header text covers only its own section and never approaches a
    # majority of pages. An absolute floor catches those; a first- or last-line
    # of a page that recurs three times is furniture, not prose.
    floor = min(3, max(2, len(pages) // 4))
    furniture = {k for k, n in counts.items()
                 if n >= threshold * len(pages) or n >= floor}
    if not furniture:
        return pages

    cleaned = []
    for page in pages:
        lines = page.splitlines()
        # Trim from the ends only; an identical sentence mid-page is content.
        while lines and (not lines[0].strip()
                         or DIGITS.sub("#", lines[0].strip()) in furniture):
            lines.pop(0)
        while lines and (not lines[-1].strip()
                         or DIGITS.sub("#", lines[-1].strip()) in furniture):
            lines.pop()
        cleaned.append("\n".join(lines))
    return cleaned


LUA = Path(__file__).parent / "fwhelp.lua"


def tables_to_gfm(md: str, unconverted: list | None = None) -> str:
    """Convert the HTML tables pymupdf4llm emits into GFM pipe tables.

    Runs through fwhelp.lua, the same filter the CHM side uses. Without it
    pandoc emits the table straight back as HTML, because pymupdf4llm attaches
    <colgroup> widths and fixed column widths force a grid table -- the identical
    problem RoboHelp's markup causes, and already solved there.
    """
    def convert(match: re.Match) -> str:
        # A GFM pipe cell cannot hold a line break, so pandoc answers any table
        # containing one with raw HTML instead -- which left 43 tables across
        # the corpus unconverted. The breaks are where the PDF happened to wrap
        # the cell text ("the analysis data control file used by<br>XAmple."),
        # so they encode page width rather than meaning and collapse to a space.
        html = re.sub(r"<br\s*/?>", " ", match.group(0))
        proc = subprocess.run(
            ["pandoc", "-f", "html", "-t", "gfm", "--wrap=none",
             f"--lua-filter={LUA}"],
            input=html, capture_output=True, text=True, encoding="utf-8",
        )
        if proc.returncode != 0 or not proc.stdout.strip() or "<table" in proc.stdout:
            # Report rather than swallow: this fallback silently hid 43 tables
            # that were shipping as raw HTML. What survives it is genuine --
            # colspan cannot be expressed as a GFM pipe table.
            if unconverted is not None:
                unconverted.append(match.group(0)[:80])
            return match.group(0)
        return "\n" + proc.stdout.strip() + "\n"

    return HTML_TABLE.sub(convert, md)


def convert_pdf(pdf: Path, out_md: Path, image_dir: Path) -> tuple[str, list, str]:
    """Convert one PDF. Returns (markdown, outline, header_strategy)."""
    doc = fitz.open(pdf)
    try:
        toc = doc.get_toc()
        if toc:
            hdr, strategy = TocHeaders(doc), "bookmarks"
        else:
            hdr, strategy = FontHeaders(doc), "font-inference"

        image_dir.mkdir(parents=True, exist_ok=True)
        chunks = pymupdf4llm.to_markdown(
            doc,
            hdr_info=hdr,
            # Per page, so running headers/footers can be found by repetition.
            # pymupdf4llm's `margins` argument does not reach this content --
            # the footer survives at every margin value tested, including one
            # far larger than the band it occupies -- so strip_furniture()
            # removes them afterwards instead.
            page_chunks=True,
            write_images=True,
            image_path=str(image_dir),
            image_format="png",
            # Skip decorative rules and page-furniture fragments; the corpus
            # PDFs use them heavily and each would otherwise become a file.
            image_size_limit=0.08,
            # "lines_strict" only, never "text": the text strategy reports a
            # 6-column x 53-row "table" for an ordinary page of prose, and
            # treats every dotted-leader contents page as tabular. Verified by
            # rendering the pages -- lines_strict finds 11 genuinely ruled
            # tables across the corpus, and they are all real.
            table_strategy="lines_strict",
            # HTML, then pandoc, because pymupdf4llm's markdown table writer
            # concatenates spans without separators: "part of Entry by default"
            # comes out as "part of Entrybydefault", silently corrupting the SFM
            # marker reference tables. Its HTML output keeps the spacing.
            table_output="html",
            show_progress=False,
        )
        pages = doc.page_count
    finally:
        doc.close()

    unconverted: list = []
    md = tables_to_gfm("\n\n".join(strip_furniture([c["text"] for c in chunks])),
                       unconverted)

    # pymupdf4llm builds image references relative to the process's working
    # directory rather than to the markdown file that holds them, so each link
    # arrives carrying the whole export path ("tools/out/pdf/X_images/...") and
    # resolves only when the file is read from that one directory -- or, when
    # --out is not under the cwd, as an absolute path that resolves nowhere
    # else at all. The images are written beside the markdown, so reduce every
    # reference to the sibling folder it actually sits in.
    md = re.sub(rf"\(\S*?{re.escape(image_dir.name)}/", f"({image_dir.name}/", md)

    # Remove the document's own contents list and back-of-book index, then push
    # its headings down one level so the title supplied by the caller is the
    # page's only H1.
    md = demote_headings(strip_back_index(strip_toc(md)))

    md = re.sub(r"\n{4,}", "\n\n\n", md).strip() + "\n"
    return md, outline_of(md), f"{strategy} ({pages}p)", unconverted


def frontmatter(fields: dict) -> str:
    def esc(v):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return str(v)
        return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = ["---"]
    for k, v in fields.items():
        if v in (None, "", [], {}):
            continue
        if isinstance(v, list):
            lines.append(f"{k}:")
            lines += [f"  - {esc(x)}" for x in v]
        else:
            lines.append(f"{k}: {esc(v)}")
    return "\n".join(lines + ["---"])


def run(repo: Path, out: Path, update: bool) -> tuple[dict, list[str]]:
    pins = json.loads(OUTLINES.read_text(encoding="utf-8")) if OUTLINES.exists() else {}
    fresh: dict[str, dict] = {}
    report: dict[str, list] = collections.defaultdict(list)
    lines: list[str] = []

    pdfs = sorted(p for p in repo.rglob("*.pdf") if ".git" not in p.parts)
    for pdf in pdfs:
        rel = pdf.relative_to(repo).as_posix()
        dest = out / (slug_path(rel)[: -len(".pdf")] + ".md")
        images = dest.parent / (dest.stem + "_images")

        try:
            md, outline, strategy, unconverted = convert_pdf(pdf, dest, images)
        except Exception as exc:
            report["pdf_failures"].append([rel, f"{type(exc).__name__}: {exc}"])
            lines.append(f"  FAILED  {rel}: {exc}")
            continue

        if unconverted:
            report["html_tables_kept"].append([rel, len(unconverted)])
        with fitz.open(pdf) as _doc:      # context manager: the handle leaked
            title = clean((_doc.metadata or {}).get("title") or "") or pdf.stem
        level1 = [t for lvl, t in outline if lvl == 1]
        fresh[rel] = {"headings": len(outline), "level1": level1}

        pin = pins.get(rel)
        if pin and not update:
            if pin["headings"] != len(outline) or pin["level1"] != level1:
                report["outline_drift"].append([
                    rel,
                    f"expected {pin['headings']} headings / {len(pin['level1'])} top-level, "
                    f"got {len(outline)} / {len(level1)}",
                ])
        elif not pin and not update:
            report["outline_unpinned"].append(rel)

        dest.parent.mkdir(parents=True, exist_ok=True)
        fm = frontmatter({
            "title": title,
            "source": rel,
            "type": "pdf",
            "headings": len(outline),
            "structure": strategy,
        })
        dest.write_text(f"{fm}\n\n# {title}\n\n{drop_repeated_title(md, title)}",
                        encoding="utf-8")

        n_img = len(list(images.glob("*"))) if images.exists() else 0
        if not n_img and images.exists():
            images.rmdir()
        lines.append(f"  {strategy:<22} {len(outline):>3} hdrs  {n_img:>4} img  {rel}")

    if update:
        OUTLINES.write_text(json.dumps(fresh, indent=1, ensure_ascii=False) + "\n",
                            encoding="utf-8")
        lines.append(f"\n  pinned {len(fresh)} outlines -> {OUTLINES}")

    return {"converted": len(fresh), "report": dict(report), "lines": lines}, lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".", type=Path)
    ap.add_argument("--out", default="export", type=Path)
    ap.add_argument("--update-outlines", action="store_true",
                    help="re-pin pdf_outlines.json after reviewing the diff")
    args = ap.parse_args()

    result, lines = run(args.repo.resolve(), args.out.resolve(), args.update_outlines)
    print("\n".join(lines))
    rep = result["report"]
    print(f"\nconverted {result['converted']} PDFs")
    for k in ("pdf_failures", "outline_drift", "outline_unpinned", "html_tables_kept"):
        v = rep.get(k, [])
        print(f"  {k:<20} {len(v)}")
        for item in v[:5]:
            print(f"      {item}")
    return 1 if rep.get("pdf_failures") or rep.get("outline_drift") else 0


if __name__ == "__main__":
    raise SystemExit(main())
