"""Convert the FieldWorks help CHM into a markdown corpus.

One markdown file per help topic, 1:1 with the authored structure. Topics are
already atomic human-authored units, so they are never merged or split -- the
consumer (the FieldWorks AI bot) chunks on its own.

Pipeline:
    extract CHM -> validate -> pandoc + fwhelp.lua -> frontmatter -> out/

Usage:
    python tools/convert.py --repo . --out out [--work DIR] [--reuse]
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urldefrag

sys.path.insert(0, str(Path(__file__).parent))
from chm_extract import extract  # noqa: E402
from survey import parse_toc  # noqa: E402
import pdf_convert  # noqa: E402

LUA = Path(__file__).parent / "fwhelp.lua"
PUBLIC = "https://downloads.languagetechnology.org/fieldworks/Documentation/en"
IMAGE_EXTS = {".gif", ".png", ".jpg", ".jpeg", ".bmp", ".svg"}


# ------------------------------------------------------------- topic facts ---

class TopicMeta(HTMLParser):
    """Pulls the bits Lua cannot see or deliberately discards.

    The "Related Topics" trailer is stripped from the body by the filter; it is
    recovered here so it survives as a link graph in frontmatter instead of
    appending a link list to 1,574 of 1,599 chunks.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self.related: list[tuple[str, str]] = []
        self.external: list[tuple[str, str]] = []
        self.links: list[str] = []
        self._in_title = False
        self._section: str | None = None
        self._href: str | None = None
        self._label: list[str] = []
        self._heading: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = a.get("name") or a.get("http-equiv")
            if key:
                self.meta[key.lower()] = a.get("content", "")
        elif re.fullmatch(r"h[1-6]", tag):
            self._heading = []
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
            if self._section:
                self._href = a["href"]
                self._label = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif re.fullmatch(r"h[1-6]", tag) and self._heading is not None:
            text = "".join(self._heading).strip()
            if text in ("Related Topics", "Related Internet Sites"):
                self._section = text
            elif text:
                self._section = None
            self._heading = None
        elif tag == "a" and self._href is not None:
            label = "".join(self._label).strip()
            target = self.related if self._section == "Related Topics" else self.external
            target.append((label, self._href))
            self._href = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._heading is not None:
            self._heading.append(data)
        if self._href is not None:
            self._label.append(data)


# ------------------------------------------------------------- conversion ---

def run_pandoc(html_text: str, tmp: Path) -> tuple[str, list[str]]:
    """Convert one topic. Returns (markdown, unmapped_span_classes)."""
    tmp.write_text(html_text, encoding="utf-8")
    proc = subprocess.run(
        ["pandoc", "-f", "html", "-t", "gfm", "--wrap=none",
         f"--lua-filter={LUA}", str(tmp)],
        capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pandoc failed: {(proc.stderr or '').strip()[:400]}")

    unmapped = []
    for line in (proc.stderr or "").splitlines():
        if line.startswith("FWHELP_UNMAPPED_SPAN"):
            unmapped += [kv.rpartition("=")[0] for kv in line.split(" ", 1)[1].split(",")]
    return proc.stdout or "", unmapped


def check_links(links: list[str], topic_rel: str, known: set[str]) -> list[str]:
    """Report internal links that point at no topic. Checked against the source
    HTML, not the rendered markdown -- the Lua Link filter does the rewriting."""
    base = Path(topic_rel).parent
    broken = []
    for href in links:
        if re.match(r"^(https?:|mailto:|file:|#)", href, re.I) or not href:
            continue
        path = urldefrag(unquote(href))[0]
        if not path or Path(path).suffix.lower() in IMAGE_EXTS:
            continue
        if _norm((base / path).as_posix()) not in known:
            broken.append(href)
    return broken


def _norm(p: str) -> str:
    parts: list[str] = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
        else:
            parts.append(seg)
    return "/".join(parts)


def frontmatter(fields: dict) -> str:
    """Minimal YAML. Values here are titles, paths and keywords -- no blocks."""
    def esc(v: str) -> str:
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
    lines.append("---")
    return "\n".join(lines)


def write_readme(out: Path, toc: list[dict], version: str, topics: int,
                 images: int, report: dict, source_ref: str, pdfs: int = 0) -> None:
    """README + full TOC tree. GitHub gives no sidebar, so this is the only
    navigation the branch has."""
    lines = [
        f"# FieldWorks Language Explorer Help {version} — Markdown",
        "",
        "Generated from `FieldWorks_Language_Explorer_Help.chm` on the "
        "[`develop`](../../tree/develop) branch. **Do not edit these files** — "
        "they are overwritten on every build. Help content is authored in "
        "Adobe RoboHelp and committed here as a compiled CHM.",
        "",
        f"- **Help version:** {version}",
        f"- **Topics:** {topics:,}   **Images:** {images:,}   **PDFs:** {pdfs}",
        f"- **Built from:** `{source_ref}`",
        "",
        "One markdown file per help topic, mirroring the CHM's own structure. "
        "Topics are never merged or split: they are already atomic, "
        "human-authored units. Each file carries YAML frontmatter with its "
        "breadcrumb, index keywords, related topics, and a link to the same "
        "topic on the "
        "[published help site](https://downloads.languagetechnology.org/fieldworks/Documentation/en/).",
        "",
        "## Quality report",
        "",
        "| Check | Count |",
        "| --- | ---: |",
    ]
    for key, label in [
        ("broken_links", "Broken internal links"),
        ("not_in_toc", "Topics missing from the TOC"),
        ("oversized", "Oversized topics (>900 words)"),
        ("undersized", "Undersized topics (<60 words)"),
        ("pdf_failures", "PDF conversion failures"),
        ("outline_drift", "PDFs whose inferred outline drifted"),
    ]:
        lines.append(f"| {label} | {len(report.get(key, []))} |")
    lines += ["", "Full detail in [`author-report.json`](author-report.json).",
              "", "## Contents", ""]

    for node in toc:
        if not node["title"]:
            continue
        indent = "  " * max(0, node["depth"] - 1)
        if node["href"]:
            target = quote(node["href"].rsplit(".", 1)[0] + ".md")
            lines.append(f"{indent}- [{node['title']}]({target})")
        else:
            lines.append(f"{indent}- **{node['title']}**")

    (out / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Keeps GitHub Pages/Jekyll from ever trying to process the branch.
    (out / ".nojekyll").write_text("", encoding="utf-8")


# ------------------------------------------------------------------- main ---

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".", type=Path)
    ap.add_argument("--out", default="out", type=Path)
    ap.add_argument("--work", default=None, type=Path)
    ap.add_argument("--reuse", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="convert only N topics (smoke test)")
    ap.add_argument("--source-ref", default="develop", help="git ref this build came from")
    args = ap.parse_args()

    repo = args.repo.resolve()
    work = (args.work or repo / ".chm-work").resolve()
    out = args.out.resolve()
    chm = repo / "FieldWorks_Language_Explorer_Help.chm"

    extraction_notes: list[str] = []
    if args.reuse and any(work.rglob("*.htm")):
        print(f"reusing extraction at {work}")
    else:
        print(f"extracted with {extract(chm, work)} -> {work}")
    extraction_notes = list(getattr(extract, "advisory", []))

    hhc = next(work.rglob("*.hhc"), None)
    hhk = next(work.rglob("*.hhk"), None)
    version = ""
    if hhk:
        m = re.search(r"_(\d+\.\d+)\.hhk$", hhk.name)
        version = m.group(1) if m else ""

    toc = parse_toc(hhc) if hhc else []
    crumbs = {n["href"]: n["breadcrumb"] for n in toc if n["href"]}

    topics = sorted(p.relative_to(work).as_posix() for p in work.rglob("*.htm"))
    known = set(topics)
    if args.limit:
        topics = topics[: args.limit]

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    tmp = out.parent / "_convert_tmp.html"
    report: dict[str, list] = collections.defaultdict(list)
    unmapped_all: collections.Counter = collections.Counter()
    written = 0

    for rel in topics:
        raw = (work / rel).read_bytes()
        text = raw.decode("cp1252", errors="replace")

        meta = TopicMeta()
        meta.feed(text)
        title = html.unescape(meta.title).strip() or Path(rel).stem.replace("_", " ")

        try:
            md, unmapped = run_pandoc(text, tmp)
        except RuntimeError as exc:
            report["pandoc_failures"].append([rel, str(exc)])
            continue
        unmapped_all.update(unmapped)

        for href in check_links(meta.links, rel, known):
            report["broken_links"].append([rel, href])

        # Drop pandoc's leading title heading; we re-emit it as the page h1.
        md = re.sub(r"^\s*#\s+.*?\n+", "", md, count=1)

        breadcrumb = crumbs.get(rel, [])
        if not breadcrumb:
            report["not_in_toc"].append(rel)
            breadcrumb = [x.replace("_", " ") for x in Path(rel).parent.parts]

        keywords = [k.strip() for k in meta.meta.get("rh-index-keywords", "").split(",") if k.strip()]
        words = len(re.sub(r"[#*`|>_\-\[\]()]", " ", md).split())
        if words > 900:
            report["oversized"].append([rel, words])
        elif words < 60 and "overview" not in Path(rel).stem.lower():
            report["undersized"].append([rel, words])

        fm = frontmatter({
            "title": title,
            "breadcrumb": breadcrumb,
            "source": rel,
            "url": f"{PUBLIC}/index.htm#t={quote(rel)}",
            "keywords": keywords,
            "related": [lbl for lbl, _ in meta.related if lbl],
            "fw_help_version": version,
            "type": "index" if "overview" in Path(rel).stem.lower() else "topic",
            "content_hash": "sha256:" + hashlib.sha256(md.encode()).hexdigest()[:16],
        })

        # Breadcrumb sits in the body, not just frontmatter: whatever chunker
        # the consumer uses, the path has to travel with the text. ~20 topics
        # are variations on "Abbreviation field" and are otherwise identical.
        trail = " › ".join(breadcrumb[:-1]) if len(breadcrumb) > 1 else ""
        header = f"{fm}\n\n# {title}\n"
        if trail:
            header += f"\n*{trail}*\n"

        dest = out / (rel.rsplit(".", 1)[0] + ".md")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(header + "\n" + md.strip() + "\n", encoding="utf-8")
        written += 1

    # Images travel with the markdown so GitHub renders them inline.
    images = 0
    for src in work.rglob("*"):
        if src.is_file() and src.suffix.lower() in IMAGE_EXTS:
            dest = out / src.relative_to(work)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            images += 1
    tmp.unlink(missing_ok=True)

    pdf_result, pdf_lines = pdf_convert.run(repo, out, update=False)
    print("\nPDFs:")
    print("\n".join(pdf_lines))
    for key, items in pdf_result["report"].items():
        report[key] = items

    if extraction_notes:
        report["stale_toc_entries"] = extraction_notes
    if unmapped_all:
        report["unmapped_span_classes"] = [[k, v] for k, v in unmapped_all.most_common()]

    write_readme(out, toc, version, written, images, report, args.source_ref,
                 pdf_result["converted"])

    (out / "author-report.json").write_text(
        json.dumps({"fw_help_version": version, "topics": written, **report}, indent=1),
        encoding="utf-8")

    print(f"\nwrote {written} markdown files + {images} images -> {out}")
    print(f"help version {version}\n")
    print("author report:")
    for k in ("pandoc_failures", "unmapped_span_classes", "pdf_failures",
              "outline_drift", "outline_unpinned", "stale_toc_entries",
              "html_tables_kept", "broken_links", "not_in_toc", "oversized", "undersized"):
        v = report.get(k, [])
        if v:
            print(f"  {k:<24} {len(v)}")
            for item in v[:5]:
                print(f"      {item}")
        else:
            print(f"  {k:<24} 0")
    # Build-breaking: the tooling lost or mangled content. Everything else --
    # broken links, over/undersized topics, stale TOC entries -- is the doc
    # author's to fix and rides along in author-report.json.
    fatal = ("pandoc_failures", "pdf_failures", "outline_drift")
    return 1 if any(report.get(k) for k in fatal) or unmapped_all else 0


if __name__ == "__main__":
    raise SystemExit(main())
