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
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import pymupdf as fitz  # "fitz" name is deprecated; alias keeps call sites short
import pymupdf4llm
from frontmatter import yaml_scalar
from output_fs import export_locks
from pymupdf4llm.helpers.pymupdf_rag import TocHeaders
from reporting import Report, make_issue
from source_safety import discover_source_files, first_link_in_path

OUTLINES = Path(__file__).parent / "pdf_outlines.json"
_WINDOWS_RESERVED_BASENAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _slug_component(part: str) -> str:
    value = part.replace(" ", "_")
    value = re.sub(r'[<>:"|?*\\]', "_", value)
    while value.endswith((".", " ")):
        value = value[:-1] + "_"
    if value.split(".", 1)[0].rstrip(" .").upper() in _WINDOWS_RESERVED_BASENAMES:
        value = "_" + value
    return value or "_"


def slug_path(rel: str) -> str:
    """Space-free output path.

    pymupdf4llm rewrites spaces to underscores when it derives image filenames
    from image_path, so a directory created as "Technical Notes_images" is not
    the one it writes into. Underscores throughout also keep the URLs clean and
    match the CHM side of the export, which RoboHelp already names that way.
    """
    return "/".join(_slug_component(part) for part in rel.split("/"))


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


# Word runs the dots together ("Introduction ....... 4"); XLingPaper/LaTeX
# spaces them out (". . . . . . . 2"). Both end in a page number.
LEADER = re.compile(r"(\.\s*){4,}\s*\d+\s*$")
CONTENTS_HEAD = re.compile(
    r"^#{1,6}\s*\**\s*(table of contents|contents|list of figures|list of tables)"
    r"\s*:?[ ]*\**\s*$", re.IGNORECASE)
INDEX_HEAD = re.compile(
    r"^#{1,6}\s*\**\s*(language|subject|topic)?\s*index\s*\**\s*$",
    re.IGNORECASE,
)
HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
EQUATION_LABEL = re.compile(r"^\(\d+\)$")


def strip_toc(md: str) -> str:
    """Drop the document's own table of contents.

    Every Word-produced PDF opens with dotted-leader contents lines, which
    pymupdf4llm renders as a bogus one-column table -- 16 to 37 lines of
    "2.1 Starting up a Project .......... 4" per document. The markdown file
    already has real headings, and this branch has a README index, so the
    inline copy is pure noise for a reader and for retrieval alike.
    """
    out = []
    lines = md.splitlines()
    front_limit = max(1, int(len(lines) * 0.25))
    for i, line in enumerate(lines):
        bare = line.strip().strip("|").strip()
        in_front_matter = i < front_limit
        if in_front_matter and LEADER.search(bare):
            continue
        # The table skeleton left behind once its rows are gone. Tested with a
        # character-set check, not a regex: a nested-quantifier pattern like
        # (:?-+:?\s*\|?)+ backtracks exponentially on a long separator row.
        # A separator is only real if an actual table row precedes it.
        if in_front_matter and bare and set(bare) <= set("|-: "):
            prev = next((x for x in reversed(out) if x.strip()), "")
            if not prev.strip().startswith("|"):
                continue
        if in_front_matter and re.fullmatch(
            r"\|\s*Contents\s*\|", line.strip(), re.IGNORECASE
        ):
            continue
        out.append(line)
    return "\n".join(out)


def strip_contents_sections(md: str) -> str:
    """Drop front-matter contents sections, whatever their rendered shape."""
    lines = md.splitlines()
    out, i = [], 0
    while i < len(lines):
        match = CONTENTS_HEAD.match(lines[i].strip())
        if match and i < len(lines) * 0.4:
            level = len(HEADING.match(lines[i]).group(1))
            end = i + 1
            while end < len(lines):
                # The TonePars paper places edition notes immediately after a
                # compact contents list. They are prose, not TOC furniture.
                if re.match(
                    r"^Editor['’]s note\b", lines[end].strip(), re.IGNORECASE
                ):
                    break
                heading = HEADING.match(lines[end])
                if heading and len(heading.group(1)) <= level:
                    break
                end += 1
            # Without a verified closing boundary, preserve the section. A
            # lower-level chapter may be the real body, and deleting to EOF is
            # worse than retaining a redundant contents list.
            if end < len(lines):
                i = end
                continue
        out.append(lines[i])
        i += 1
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


def markdown_label(line: str) -> str:
    """Plain text from one simple Markdown heading or emphasized line."""
    text = re.sub(r"^#{1,6}\s+", "", line.strip())
    text = clean(re.sub(r"[*_`]", "", text))
    return re.sub(r"\s+([:;,])", r"\1", text)


def pick_title(meta_title: str, md: str, stem: str) -> str:
    """Choose useful metadata, a document heading, or the curated filename."""
    stem = stem.replace("_", " ").strip()
    bad = re.compile(
        r"\.(doc|pdf|rtf)x?\b|^microsoft word\b|\breadme\b", re.IGNORECASE
    )

    candidate = clean(meta_title)
    if candidate and not bad.search(candidate) and 4 < len(candidate) < 120:
        if candidate.lower() in stem.lower() and len(candidate) < len(stem):
            return stem
        return candidate

    generic = {"contents", "table of contents", "list of figures", "list of tables"}
    for line in md.splitlines()[:24]:
        text = markdown_label(line)
        if not text or text.rstrip(":").lower() in generic:
            continue
        if re.match(r"^\d+(?:\.\d+)*\s+(?=\S)", text) or text.isdigit():
            continue
        heading = HEADING.match(line)
        if heading:
            if 4 < len(text) < 120 and not bad.search(text):
                return text
        # Some title pages have no bookmark or heading style. Accept a title
        # line before falling through to the generic Contents bookmark.
        elif 12 < len(text) < 120 and not bad.search(text):
            return text
    return stem


def drop_repeated_title(md: str, title: str) -> str:
    """Remove the document's own title heading when it restates the page title.

    Otherwise every PDF opens with the title twice -- once as the H1 this tool
    adds, once as the heading from the PDF's title page ("Technical Notes on
    FieldWorks Send-Receive" then "Technical Notes on Fieldworks Send/Receive").
    Compared on letters and digits alone, so punctuation and casing differences
    like Send-Receive vs Send/Receive still count as the same title.
    """
    key = lambda s: re.sub(r"[^a-z0-9]", "", markdown_label(s).lower())
    want = key(title)
    lines = md.splitlines()

    # A title may be a plain emphasized line, a heading, or a heading followed
    # by a separately styled continuation. Remove every opening copy: the
    # Conceptual Introduction PDF contains the same split title twice.
    while True:
        found = False
        for start in range(min(len(lines), 24)):
            if not lines[start].strip():
                continue
            joined = ""
            used = 0
            for end in range(start, min(len(lines), start + 8)):
                if not lines[end].strip():
                    continue
                joined += key(lines[end])
                used += 1
                if joined == want:
                    del lines[start:end + 1]
                    while start < len(lines) and not lines[start].strip():
                        del lines[start]
                    found = True
                    break
                if used >= 3 or not want.startswith(joined):
                    break
            if found:
                break
        if not found:
            break
    return "\n".join(lines).strip() + "\n"


def normalize_pdf_headings(md: str) -> str:
    """Remove audited false headings and make the first real level H2.

    Some bookmark trees promote author bylines and equation numbers to
    headings. The former only occurs as a short title-page heading at level
    four or deeper; the latter is unambiguously a standalone parenthesized
    number. After those are removed, shift an otherwise valid tree so its
    shallowest heading is the PDF body's H2.
    """
    lines = md.splitlines()
    headings: list[tuple[int, int, str]] = []
    fenced = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = HEADING.match(line)
        if match:
            headings.append((i, len(match.group(1)), clean(match.group(2))))

    remove: set[int] = set()
    for i, level, text in headings:
        if EQUATION_LABEL.fullmatch(text):
            remove.add(i)

    remaining = [(i, level, text) for i, level, text in headings if i not in remove]
    if remaining:
        first_i, first_level, first_text = remaining[0]
        words = first_text.split()
        looks_like_name = (
            first_i < 8 and first_level >= 4 and 2 <= len(words) <= 5
            and all(word[:1].isupper() for word in words if word)
            and not any(char.isdigit() for char in first_text)
        )
        if looks_like_name:
            remove.add(first_i)
            remaining = remaining[1:]

    shift = 2 - min((level for _, level, _ in remaining), default=2)
    out: list[str] = []
    for i, line in enumerate(lines):
        if i in remove:
            continue
        match = HEADING.match(line)
        if not match:
            out.append(line)
            continue
        level = max(1, len(match.group(1)) + shift)
        out.append("#" * level + line[len(match.group(1)):])
    return "\n".join(out)


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


def normalize_outline(outline: list[tuple[int, str]] | list[list]) -> list[list]:
    """Return the ordered, comparable representation used by outline locks."""
    return [[int(level), clean(str(text))] for level, text in outline]


def outline_matches(pin: dict, outline: list[tuple[int, str]] | list[list]) -> bool:
    """Compare every normalized heading, in order, with a pinned outline."""
    expected = pin.get("outline")
    if not isinstance(expected, list):
        # Legacy count/level1 pins are intentionally not sufficient locks.
        return False
    return expected == normalize_outline(outline)


def finalize_pdf(meta_title: str, md: str, stem: str) -> tuple[str, str, list]:
    """Select the title and derive structure from the body that will be emitted."""
    title = pick_title(meta_title, md, stem)
    body = normalize_pdf_headings(drop_repeated_title(md, title))
    return title, body, outline_of(body)


PAGE_NUMBER = re.compile(r"^\d+$")
HTML_TABLE = re.compile(r"<table\b.*?</table>", re.DOTALL | re.IGNORECASE)


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

    def key(line: str) -> str:
        line = line.strip()
        return "#page-number" if PAGE_NUMBER.fullmatch(line) else line

    counts: collections.Counter = collections.Counter()
    samples: dict[str, str] = {}
    for page in pages:
        lines = [ln.strip() for ln in page.splitlines() if ln.strip()]
        for line in set(lines[:2] + lines[-2:]):
            normalized = key(line)
            counts[normalized] += 1
            samples.setdefault(normalized, line)

    # Word repeats the *current section* heading in the running header, so any
    # one header text may cover only two pages. Accept two occurrences only for
    # explicit Markdown headings and page numbers. Plain prose needs both three
    # occurrences and a majority of pages, preventing repeated instructions or
    # distinct numbered headings from being collapsed into furniture.
    need = max(3, int(threshold * len(pages) + 0.999))
    furniture = {
        k for k, n in counts.items()
        if ((k == "#page-number" or HEADING.match(samples[k])) and n >= 2)
        or n >= need
    }
    if not furniture:
        return pages

    cleaned = []
    for page in pages:
        lines = page.splitlines()
        # Trim from the ends only; an identical sentence mid-page is content.
        while lines and (not lines[0].strip()
                         or key(lines[0]) in furniture):
            lines.pop(0)
        while lines and (not lines[-1].strip()
                         or key(lines[-1]) in furniture):
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
            check=False,
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


def convert_pdf(pdf: Path, out_md: Path, image_dir: Path) -> tuple[str, str, list]:
    """Convert one PDF and retain source replacement-character provenance."""
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
        source_replacements = []
        for page_number, page in enumerate(doc, 1):
            details = source_replacement_details(page.get_text())
            if details:
                source_replacements.append({"page": page_number, **details})
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
    md = demote_headings(strip_back_index(strip_contents_sections(strip_toc(md))))

    md = re.sub(r"\n{4,}", "\n\n\n", md).strip() + "\n"
    if source_replacements:
        unconverted.append({"kind": "source_replacement", "pages": source_replacements})
    return md, f"{strategy} ({pages}p)", unconverted


def frontmatter(fields: dict) -> str:
    lines = ["---"]

    def emit(k, v, indent=""):
        if v in (None, "", [], {}):
            return
        if isinstance(v, dict):
            lines.append(f"{indent}{k}:")
            for child, value in v.items():
                emit(child, value, indent + "  ")
        elif isinstance(v, list):
            lines.append(f"{indent}{k}:")
            for item in v:
                lines.append(f"{indent}  - {yaml_scalar(item)}")
        else:
            lines.append(f"{indent}{k}: {yaml_scalar(v)}")

    for k, v in fields.items():
        emit(k, v)
    return "\n".join(lines + ["---"])


PDF_MANIFEST = ".pdf-converter-manifest.json"
PDF_MANIFEST_SCHEMA = 2


class ManifestError(ValueError):
    """A PDF manifest is malformed or does not describe canonical outputs."""


def _validate_lock_path(path: Path) -> Path:
    """Reject linked or parent-traversing lock paths before any lock I/O."""
    path = Path(path)
    if ".." in path.parts:
        raise ManifestError(f"unsafe lock path: {path}")
    if first_link_in_path(path) is not None:
        raise ManifestError(f"lock path contains symlink/junction: {path}")
    return path


def _manifest_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError(f"duplicate manifest key: {key!r}")
        result[key] = value
    return result


def _safe_manifest_relative(value: object, label: str) -> str:
    if (not isinstance(value, str) or not value or "\\" in value
            or "\x00" in value or ":" in value):
        raise ManifestError(f"unsafe {label} path: {value!r}")
    if any(ord(char) < 32 for char in value):
        raise ManifestError(f"unsafe {label} path: {value!r}")
    path = Path(value)
    if value.startswith(("/", "\\")) or path.drive:
        raise ManifestError(f"absolute {label} path: {value!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ManifestError(f"traversal {label} path: {value!r}")
    return value


def _manifest_destinations(source: str) -> tuple[str, str]:
    if not source.casefold().endswith(".pdf"):
        raise ManifestError(f"manifest source is not a PDF: {source!r}")
    rel_dest = slug_path(source)[:-4] + ".md"
    rel_images = (Path(rel_dest).parent / (Path(rel_dest).stem + "_images")).as_posix()
    return rel_dest, rel_images


def _ownership_key(relative: str) -> str:
    """Return the host filesystem's normalized key for a safe relative path."""
    return os.path.normcase(os.path.normpath(relative))


def validate_manifest(manifest: Mapping, out: Path, *,
                      authenticated_images: set[str] | None = None) -> dict[str, dict]:
    """Validate a complete schema-2 manifest before any output mutation."""
    if not isinstance(manifest, dict) or set(manifest) != {"schema", "files"}:
        raise ManifestError("manifest must contain exactly schema and files")
    if type(manifest["schema"]) is not int or manifest["schema"] != PDF_MANIFEST_SCHEMA:
        raise ManifestError("unsupported PDF manifest schema")
    files = manifest["files"]
    if not isinstance(files, dict):
        raise ManifestError("manifest files must be an object")
    root = Path(os.path.abspath(os.fspath(out)))
    authenticated_image_keys = {
        _ownership_key(value) for value in (authenticated_images or set())
        if isinstance(value, str)
    }
    seen_sources: set[str] = set()
    seen_destinations: set[str] = set()
    normalized: dict[str, dict] = {}
    for source, entry in files.items():
        source = _safe_manifest_relative(source, "source")
        source_key = source.casefold()
        if source_key in seen_sources:
            raise ManifestError(f"case-colliding manifest source: {source!r}")
        seen_sources.add(source_key)
        if not isinstance(entry, dict) or set(entry) != {"markdown", "images"}:
            raise ManifestError(f"invalid manifest entry for {source!r}")
        expected_markdown, expected_images = _manifest_destinations(source)
        markdown = _safe_manifest_relative(entry["markdown"], "markdown")
        if markdown != expected_markdown:
            raise ManifestError(f"markdown destination mismatch for {source!r}")
        images = entry["images"]
        if images is not None:
            images = _safe_manifest_relative(images, "images")
            if images != expected_images:
                raise ManifestError(f"images destination mismatch for {source!r}")
        elif first_link_in_path(root / expected_images) is not None:
            raise ManifestError(f"images destination contains symlink/junction: {source!r}")
        elif (root / expected_images).exists() and _ownership_key(expected_images) not in authenticated_image_keys:
            raise ManifestError(f"null images destination has existing output: {source!r}")
        for destination in (markdown, images):
            if destination is None:
                continue
            key = destination.casefold()
            if key in seen_destinations:
                raise ManifestError(f"case-colliding manifest destination: {destination!r}")
            seen_destinations.add(key)
            if first_link_in_path(root / destination) is not None:
                raise ManifestError(f"manifest destination contains symlink/junction: {destination!r}")
            if _owned_path(root, destination) is None:
                raise ManifestError(f"manifest destination escapes output root: {destination!r}")
        normalized[source] = {"markdown": markdown, "images": images}
    return normalized


def _load_manifest(path: Path, out: Path) -> tuple[dict, dict[str, dict]]:
    if first_link_in_path(path) is not None:
        raise ManifestError("PDF manifest path contains symlink/junction")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_manifest_pairs)
    except ManifestError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read PDF manifest: {exc}") from exc
    return manifest, validate_manifest(manifest, out)


def _normalize_root(path: Path) -> Path:
    """Normalize lexical ``..`` segments without resolving symlinks."""
    return Path(os.path.normpath(os.fspath(Path(path).expanduser())))


def discover_pdfs(repo: Path) -> list[Path]:
    """Discover PDF inputs independent of filename case on the host OS."""
    repo = _normalize_root(repo)
    return discover_source_files(
        repo, suffixes={".pdf"}, recursive=True, exclude_dirs={".git"}
    )


def destination_collisions(repo: Path, out: Path,
                           pdfs: list[Path] | None = None) -> list[tuple[str, list[str]]]:
    """Find PDFs whose normalized destination path is claimed more than once."""
    repo = _normalize_root(repo)
    pdfs = pdfs if pdfs is not None else discover_pdfs(repo)
    claimed: dict[str, tuple[str, list[str]]] = {}
    for pdf in pdfs:
        rel = pdf.relative_to(repo).as_posix()
        dest = out / (slug_path(rel)[: -len(".pdf")] + ".md")
        display = dest.relative_to(out).as_posix()
        key = display.casefold()
        if key not in claimed:
            claimed[key] = (display, [])
        claimed[key][1].append(rel)
    return [
        (display, sorted(rels))
        for display, rels in sorted(claimed.values()) if len(rels) > 1
    ]


def _owned_path(out: Path, relative: str) -> Path | None:
    """Return a lexical in-root path, refusing link chains."""
    candidate = Path(os.path.abspath(os.fspath(Path(out) / relative)))
    root = Path(os.path.abspath(os.fspath(out)))
    if first_link_in_path(candidate) is not None:
        raise ManifestError(f"manifest path contains symlink/junction: {relative!r}")
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _validate_regular_file(path: Path, label: str) -> None:
    if first_link_in_path(path) is not None or not path.exists() or not path.is_file():
        raise ManifestError(f"{label} must be an existing regular non-link file: {path}")


def _validate_clean_directory(path: Path, label: str) -> None:
    if first_link_in_path(path) is not None or not path.exists() or not path.is_dir():
        raise ManifestError(f"{label} must be an existing non-link directory: {path}")
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise ManifestError(f"cannot inspect {label}: {current}") from exc
        for entry in entries:
            child = Path(entry.path)
            if first_link_in_path(child) is not None:
                raise ManifestError(f"{label} contains symlink/junction: {child}")
            if entry.is_dir(follow_symlinks=False):
                pending.append(child)
            elif not entry.is_file(follow_symlinks=False):
                raise ManifestError(f"{label} contains non-regular entry: {child}")


def _validate_manifest_outputs(out: Path, stage: Path,
                               previous: dict[str, dict], current: dict[str, dict]) -> None:
    """Validate all filesystem objects before staging or replacing anything."""
    for files in previous.values():
        if not isinstance(files, dict):
            continue
        markdown = files.get("markdown")
        if isinstance(markdown, str):
            path = _owned_path(out, markdown)
            if path is None:
                raise ManifestError(f"prior markdown escapes output: {markdown!r}")
            _validate_regular_file(path, "prior markdown")
        images = files.get("images")
        if isinstance(images, str):
            path = _owned_path(out, images)
            if path is None:
                raise ManifestError(f"prior images escape output: {images!r}")
            _validate_clean_directory(path, "prior images")

    prior_path_keys = {
        _ownership_key(value)
        for files in previous.values()
        if isinstance(files, dict)
        for value in (files.get("markdown"), files.get("images"))
        if isinstance(value, str)
    }
    for files in current.values():
        if not isinstance(files, dict):
            continue
        markdown = files.get("markdown")
        if isinstance(markdown, str):
            staged = _owned_path(stage, markdown)
            if staged is None:
                raise ManifestError(f"current markdown escapes stage: {markdown!r}")
            _validate_regular_file(staged, "current staged markdown")
            target = _owned_path(out, markdown)
            if target is not None and target.exists() and _ownership_key(markdown) not in prior_path_keys:
                raise FileExistsError(f"PDF destination is not converter-owned: {markdown}")
        images = files.get("images")
        if isinstance(images, str):
            staged = _owned_path(stage, images)
            if staged is None:
                raise ManifestError(f"current images escape stage: {images!r}")
            _validate_clean_directory(staged, "current staged images")
            target = _owned_path(out, images)
            if target is not None and target.exists() and _ownership_key(images) not in prior_path_keys:
                raise FileExistsError(f"PDF destination is not converter-owned: {images}")


def _remove_owned_entry(out: Path, files: dict) -> None:
    for key in ("markdown", "images"):
        value = files.get(key)
        path = _owned_path(out, value) if isinstance(value, str) else None
        if path is None or not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def _promote_pdf_outputs_locked(out: Path, stage: Path,
                        previous: dict, current: dict,
                        lock_path: Path | None = None,
                        fresh: dict | None = None) -> None:
    """Replace the complete converter-owned PDF set from a finished stage."""
    out = Path(out)
    if lock_path is not None:
        lock_path = _validate_lock_path(lock_path)
    manifest_path = out / PDF_MANIFEST
    # Validate the on-disk manifest before even creating a backup. The caller's
    # parsed view is also checked so a stale/tampered in-memory view cannot
    # authorize a different deletion set.
    if first_link_in_path(manifest_path) is not None:
        raise ManifestError("PDF manifest path contains symlink/junction")
    if manifest_path.exists():
        _, actual_previous = _load_manifest(manifest_path, out)
        if previous != actual_previous:
            raise ManifestError("previous PDF manifest does not match on-disk manifest")
    else:
        actual_previous = validate_manifest(
            {"schema": PDF_MANIFEST_SCHEMA, "files": previous}, out
        )
    previous = actual_previous
    prior_images = {
        _ownership_key(files["images"]) for files in previous.values()
        if isinstance(files, dict) and isinstance(files.get("images"), str)
    }
    validate_manifest(
        {"schema": PDF_MANIFEST_SCHEMA, "files": current}, out,
        authenticated_images=prior_images,
    )
    _validate_manifest_outputs(out, stage, previous, current)
    out.mkdir(parents=True, exist_ok=True)
    staged_manifest = stage / PDF_MANIFEST
    if first_link_in_path(staged_manifest) is not None:
        raise ManifestError("staged PDF manifest path contains symlink/junction")
    staged_manifest.write_text(
        json.dumps({"schema": PDF_MANIFEST_SCHEMA, "files": current}, indent=1,
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    staged_lock = stage / ".pdf-outlines.json"
    if lock_path is not None:
        _validate_lock_path(lock_path)
        if first_link_in_path(staged_lock) is not None:
            raise ManifestError("staged PDF lock path contains symlink/junction")
        staged_lock.write_text(
            json.dumps(fresh or {}, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    # Validate targets before removing anything. Existing paths are safe to
    # replace only when the prior manifest claimed them; CHM/unrelated output
    # must never be overwritten by a PDF conversion.
    prior_paths = {
        value
        for files in previous.values()
        if isinstance(files, dict)
        for value in (files.get("markdown"), files.get("images"))
        if isinstance(value, str)
    }

    backup = Path(tempfile.mkdtemp(prefix=".pdf-backup-", dir=out.parent))
    backed_up: list[tuple[str, Path]] = []
    manifest_backup = backup / PDF_MANIFEST
    had_manifest = manifest_path.exists()
    lock_backup = backup / "pdf-outlines.json"
    if lock_path is not None:
        _validate_lock_path(lock_path)
    had_lock = lock_path is not None and lock_path.exists()
    try:
        if had_manifest:
            shutil.copy2(manifest_path, manifest_backup)
        if had_lock and lock_path is not None:
            shutil.copy2(lock_path, lock_backup)

        # Keep a recoverable copy until every staged path has been promoted.
        for value in prior_paths:
            source = _owned_path(out, value)
            target = _owned_path(backup, value)
            if source is None or target is None or not source.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
            backed_up.append((value, target))

        # Remove every prior owned path, including the same source's image
        # folder: staging is a complete replacement, so omitted files die.
        for files in previous.values():
            if isinstance(files, dict):
                _remove_owned_entry(out, files)

        for files in current.values():
            if not isinstance(files, dict):
                continue
            for key in ("markdown", "images"):
                value = files.get(key)
                if not isinstance(value, str):
                    continue
                source = _owned_path(stage, value)
                if source is None or not source.exists():
                    continue
                if source.is_dir() and not any(source.iterdir()):
                    continue
                target = _owned_path(out, value)
                if target is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
        shutil.move(str(staged_manifest), str(manifest_path))
        if lock_path is not None:
            _validate_lock_path(lock_path)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged_lock), str(lock_path))
    except Exception:
        # A failed move must not leave a half-promoted PDF set behind.
        for files in current.values():
            if isinstance(files, dict):
                _remove_owned_entry(out, files)
        for value, source in backed_up:
            target = _owned_path(out, value)
            if target is None or not source.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        if had_manifest and manifest_backup.exists():
            manifest_path.unlink(missing_ok=True)
            shutil.copy2(manifest_backup, manifest_path)
        elif not had_manifest:
            manifest_path.unlink(missing_ok=True)
        if lock_path is not None:
            if had_lock and lock_backup.exists():
                lock_path.unlink(missing_ok=True)
                shutil.copy2(lock_backup, lock_path)
            elif not had_lock:
                lock_path.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)


def promote_pdf_outputs(out: Path, stage: Path,
                        previous: dict, current: dict,
                        lock_path: Path | None = None,
                        fresh: dict | None = None) -> None:
    """Promote PDF output while serializing direct destination mutation."""
    out = Path(out)
    if lock_path is not None:
        # Keep the same output-then-global order as ``run`` when updating the
        # shared outline lock.  The locked implementation is used internally
        # by ``run`` to avoid recursive acquisition.
        with export_locks(out, OUTLINES):
            _promote_pdf_outputs_locked(
                out, stage, previous, current, lock_path=lock_path, fresh=fresh,
            )
    else:
        with export_locks(out):
            _promote_pdf_outputs_locked(
                out, stage, previous, current, lock_path=lock_path, fresh=fresh,
            )


def _source_url(rel: str, resolver=None) -> str:
    """Resolve a stable source URL/ref through the small orchestration seam."""
    encoded = "/".join(quote(part, safe="") for part in rel.split("/"))
    if resolver is None:
        value = encoded
    elif callable(resolver):
        value = str(resolver(rel))
    elif isinstance(resolver, dict):
        value = str(resolver.get(rel, encoded))
    else:
        value = str(resolver)
        if "{path}" in value:
            value = value.format(path=encoded)
        else:
            value = value.rstrip("/") + "/" + encoded
    if "{path}" in value:
        value = value.format(path=encoded)
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        value = urlunsplit((
            parts.scheme,
            parts.netloc,
            quote(parts.path, safe="/%:@-._~!$&'()*+,;=%"),
            parts.query,
            parts.fragment,
        ))
    else:
        value = quote(value, safe="/%:@-._~!$&'()*+,;=%")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def replacement_provenance(source_pages: list[dict], generated_count: int) -> dict:
    """Classify replacement characters as source-derived or exporter-created."""
    source_count = sum(int(item.get("count", 0)) for item in source_pages)
    return {
        "source_count": source_count,
        "exporter_count": max(0, generated_count - source_count),
    }


def source_replacement_details(text: str) -> dict | None:
    """Identify source glyphs likely to become replacement characters."""
    chars = [
        char for char in text
        if char == "\ufffd" or (ord(char) < 32 and char not in "\t\n\r")
    ]
    if not chars:
        return None
    return {
        "count": len(chars),
        "codepoints": sorted({f"U+{ord(char):04X}" for char in chars}),
    }


def _run_locked(repo: Path, out: Path, update: bool, source_url=None,
        source_ref=None) -> tuple[dict, list[str]]:
    """Convert all PDFs, with ``source_url`` as the repository policy seam.

    ``source_url`` may be a URL template containing ``{path}``, a mapping, or
    a callable receiving the repository-relative PDF path. ``source_ref`` is a
    backwards-compatible alias for callers that prefer that terminology.
    """
    repo = _normalize_root(repo)
    if source_ref is not None:
        source_url = source_ref
    _validate_lock_path(OUTLINES)
    pins = json.loads(OUTLINES.read_text(encoding="utf-8")) if OUTLINES.exists() else {}
    fresh: dict[str, dict] = {}
    report: dict[str, list] = collections.defaultdict(list)
    lines: list[str] = []

    pdfs = discover_pdfs(repo)
    collisions = destination_collisions(repo, out, pdfs)
    if collisions:
        report["destination_collisions"].extend(collisions)
        lines.extend(f"  COLLISION {dest}: {', '.join(rels)}" for dest, rels in collisions)
        return {"converted": 0, "report": dict(report), "lines": lines}, lines

    out.parent.mkdir(parents=True, exist_ok=True)
    previous: dict = {}
    manifest_path = out / PDF_MANIFEST
    if first_link_in_path(manifest_path) is not None:
        raise ManifestError("PDF manifest path contains symlink/junction")
    if manifest_path.exists():
        _, previous = _load_manifest(manifest_path, out)
    current: dict = {}
    stage = Path(tempfile.mkdtemp(prefix=".pdf-convert-", dir=out.parent))

    try:
        for pdf in pdfs:
            rel = pdf.relative_to(repo).as_posix()
            rel_dest = slug_path(rel)[: -len(".pdf")] + ".md"
            dest = stage / rel_dest
            images = dest.parent / (dest.stem + "_images")

            try:
                md, strategy, unconverted = convert_pdf(pdf, dest, images)
            # A single damaged PDF is reportable while the remaining corpus
            # continues through staging; this boundary intentionally catches
            # converter/backend errors of varying concrete types.
            except Exception as exc:  # noqa: BLE001
                report["pdf_failures"].append([rel, f"{type(exc).__name__}: {exc}"])
                lines.append(f"  FAILED  {rel}: {exc}")
                continue

            table_warnings = [item for item in unconverted if not isinstance(item, dict)]
            source_warnings = [
                item for item in unconverted
                if isinstance(item, dict) and item.get("kind") == "source_replacement"
            ]
            if table_warnings:
                report["html_tables_kept"].append([rel, len(table_warnings)])
            source_pages = [
                page for warning in source_warnings for page in warning["pages"]
            ]
            provenance = replacement_provenance(source_pages, md.count("\ufffd"))
            if provenance["source_count"]:
                report["pdf_source_replacements"].append([rel, source_pages])
            if provenance["exporter_count"]:
                report["pdf_export_replacements"].append([rel, provenance])
            with fitz.open(pdf) as _doc:
                metadata = dict(_doc.metadata or {})
                meta_title = metadata.get("title") or ""
            title, body, outline = finalize_pdf(meta_title, md, pdf.stem)
            normalized_outline = normalize_outline(outline)
            fresh[rel] = {"outline": normalized_outline, "headings": len(outline)}

            pin = pins.get(rel)
            if not update:
                if pin is None:
                    report["outline_unpinned"].append(rel)
                elif not outline_matches(pin, outline):
                    expected = pin.get("outline", "<legacy lock>")
                    report["outline_drift"].append([
                        rel,
                        f"expected ordered outline {expected!r}, got {normalized_outline!r}",
                    ])

            dest.parent.mkdir(parents=True, exist_ok=True)
            fm = frontmatter({
                "title": title,
                "source": rel,
                "source_url": _source_url(rel, source_url),
                "sha256": _sha256(pdf),
                "pdf_metadata": metadata,
                "type": "pdf",
                "outline_count": len(outline),
                "structure": strategy,
            })
            dest.write_text(f"{fm}\n\n# {title}\n\n{body}",
                            encoding="utf-8")
            n_img = len(list(images.glob("*"))) if images.exists() else 0
            if not n_img and images.exists():
                images.rmdir()
            current[rel] = {
                "markdown": rel_dest,
                "images": (
                    (Path(rel_dest).parent / (Path(rel_dest).stem + "_images")).as_posix()
                    if n_img else None
                ),
            }
            lines.append(f"  {strategy:<22} {len(outline):>3} hdrs  {n_img:>4} img  {rel}")

        # No final output or manifest is touched until every PDF and every lock
        # check succeeds. A late failure therefore leaves the prior PDF set.
        lock_fatal = (report.get("outline_drift")
                      or report.get("outline_unpinned")) and not update
        fatal = (report.get("pdf_failures") or lock_fatal
                 or report.get("pdf_export_replacements"))
        if not fatal and not report.get("pdf_failures"):
            _promote_pdf_outputs_locked(
                out, stage, previous, current,
                lock_path=OUTLINES if update else None,
                fresh=fresh if update else None,
            )
            if update:
                lines.append(f"\n  pinned {len(fresh)} outlines -> {OUTLINES}")
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    return {"converted": len(fresh), "report": dict(report), "lines": lines}, lines


def run_in_private_stage(repo: Path, out: Path, update: bool, source_url=None,
        source_ref=None) -> tuple[dict, list[str]]:
    """Convert into a caller-owned private stage while locking global state.

    The caller must guarantee that ``out`` is an unshared staging path.  Such
    a path needs no destination lock; creating one beside it would turn the
    lockfile into generated content when the enclosing stage is promoted.
    """
    with export_locks(OUTLINES):
        return _run_locked(
            repo, Path(out), update, source_url=source_url, source_ref=source_ref,
        )


def run(repo: Path, out: Path, update: bool, source_url=None,
        source_ref=None) -> tuple[dict, list[str]]:
    """Convert PDFs under output-then-global-outline locks.

    The global outline lock covers the pinned-outline read and any update
    promotion, including the interval between those operations.
    """
    out = Path(out)
    with export_locks(out, OUTLINES):
        return _run_locked(
            repo, out, update, source_url=source_url, source_ref=source_ref,
        )


def _canonical_report(producer_report: dict) -> Report:
    """Render producer findings through the shared issue catalog."""
    issues = []
    for code, entries in producer_report.items():
        for item in entries if isinstance(entries, list) else [entries]:
            if isinstance(item, (list, tuple)) and item:
                path = str(item[0])
                message = str(item[1]) if len(item) > 1 else ""
            else:
                path = ""
                message = str(item)
            issues.append(make_issue(code, message, path, item))
    return Report(issues)


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
    canonical = _canonical_report(rep)
    print(canonical.to_console())
    return 1 if canonical.fatal else 0


if __name__ == "__main__":
    raise SystemExit(main())
