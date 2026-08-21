"""Reusable conversion of one extracted CHM into a namespaced Markdown tree."""

from __future__ import annotations

import hashlib
import html
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote, unquote, urldefrag

from chm_extract import extract, validate
from chm_metadata import TopicMeta, parse_toc, safe_stem

LUA = Path(__file__).with_name("fwhelp.lua")
IMAGE_EXTS = {".gif", ".png", ".jpg", ".jpeg", ".bmp", ".svg", ".ico"}


def _normalize_source(source: str) -> str:
    """Normalize RoboHelp NBSP spacing at the HTML-to-Markdown boundary.

    RoboHelp uses CP1252 byte 0xA0 and HTML NBSP entities for both layout
    spacing and empty table cells. Markdown/Pandoc can emit U+FFFD for these
    values, so ordinary spaces preserve word separation without retaining an
    unsafe format-specific spacing character.
    """
    source = source.replace("\u00a0", " ")
    return re.sub(r"(?i)&nbsp;|&#160;", " ", source)


def _norm(path: str) -> str:
    parts: list[str] = []
    for segment in path.replace("\\", "/").split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if parts:
                parts.pop()
        else:
            parts.append(segment)
    return "/".join(parts)


def _relative(source: str, target: str) -> str:
    base = Path(source).parent.parts
    parts = Path(target).with_suffix(".md").parts
    common = 0
    while common < len(base) and common < len(parts) and base[common] == parts[common]:
        common += 1
    return "/".join(("..",) * (len(base) - common) + parts[common:])


def frontmatter(fields: dict) -> str:
    def esc(value: object) -> str:
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = ["---"]
    for key, value in fields.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {esc(item)}" for item in value)
        else:
            lines.append(f"{key}: {esc(value)}")
    lines.append("---")
    return "\n".join(lines)


def run_pandoc(html_text: str, tmp: Path) -> tuple[str, list[str]]:
    tmp.write_text(html_text, encoding="utf-8")
    proc = subprocess.run(
        ["pandoc", "-f", "html", "-t", "gfm", "--wrap=none",
         f"--lua-filter={LUA}", str(tmp)],
        capture_output=True, text=True, encoding="utf-8",
        check=False,
    )
    if proc.returncode:
        raise RuntimeError(f"pandoc failed: {(proc.stderr or '').strip()[:400]}")
    unmapped: list[str] = []
    for line in (proc.stderr or "").splitlines():
        if line.startswith("FWHELP_UNMAPPED_SPAN"):
            unmapped.extend(part.split("=", 1)[0] for part in line.split(" ", 1)[1].split(","))
    return _normalize_nested_lists(proc.stdout or ""), unmapped


def _normalize_nested_lists(markdown: str) -> str:
    """Repair Pandoc's literal ``- -`` spelling without dropping list items."""
    return re.sub(r"^(\s*)-\s+-\s+", lambda m: m.group(1) + "  - ", markdown, flags=re.MULTILINE)


def _check_links(links: list[str], topic_rel: str, known: set[str]) -> list[str]:
    broken = []
    for href in links:
        if re.match(r"^(?:https?:|mailto:|file:|javascript:|#)", href, re.IGNORECASE):
            continue
        path = urldefrag(unquote(href))[0]
        if not path or Path(path).suffix.lower() in IMAGE_EXTS:
            continue
        target = _norm((Path(topic_rel).parent / path).as_posix())
        if target not in known:
            broken.append(href)
    return broken


def _version(extraction: Path) -> str:
    for path in sorted(extraction.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file() or path.suffix.lower() != ".hhk":
            continue
        match = re.search(r"_(\d+\.\d+)\.hhk$", path.name)
        if match:
            return match.group(1)
    return ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _topic_files(extraction: Path) -> list[Path]:
    return sorted(
        (path for path in extraction.rglob("*")
         if path.is_file() and path.suffix.lower() in {".htm", ".html"}),
        key=lambda path: (
            path.relative_to(extraction).as_posix().casefold(),
            path.relative_to(extraction).as_posix(),
        ),
    )


def convert_chm(chm: Path, work_root: Path, destination: Path, *, reuse: bool = False,
                limit: int = 0, source_ref: str = "develop", source_url_base: str | None = None,
                extractor=extract, extract_fn=None) -> dict:
    """Extract and convert one CHM, returning report facts and TOC nodes."""
    chm = Path(chm)
    if extract_fn is not None:
        extractor = extract_fn
    extraction = Path(work_root) / safe_stem(chm.name)
    advisory: list[str] = []
    if reuse and _topic_files(extraction):
        fatal, advisory = validate(extraction)
        if fatal:
            raise RuntimeError("reused extraction failed validation: " + "; ".join(fatal))
    else:
        extractor(chm, extraction)
        advisory = list(getattr(extractor, "advisory", []))
    hhc = next((path for path in sorted(extraction.rglob("*"), key=lambda item: item.as_posix().casefold())
                if path.is_file() and path.suffix.lower() == ".hhc"), None)
    toc = parse_toc(hhc) if hhc else []
    crumbs = {node["href"]: node["breadcrumb"] for node in toc if node["href"]}
    version = _version(extraction)
    source_hash = "sha256:" + _sha256(chm)
    topics = [path.relative_to(extraction).as_posix() for path in _topic_files(extraction)]
    known = set(topics)
    if limit:
        topics = topics[:limit]
    destination.mkdir(parents=True, exist_ok=True)
    claimed: dict[str, list[str]] = defaultdict(list)
    for rel in topics:
        claimed[Path(rel).with_suffix(".md").as_posix().casefold()].append(rel)
    collisions = [(dest, paths) for dest, paths in sorted(claimed.items()) if len(paths) > 1]
    if collisions:
        report: dict[str, list] = {"destination_collisions": collisions}
        return {"chm": chm.name, "stem": safe_stem(chm.name), "version": _version(extraction),
                "toc": toc, "topics": 0, "images": 0, "topics_paths": [], "report": report}
    tmp = destination.parent / f".{safe_stem(chm.name)}-pandoc.html"
    report: dict[str, list] = defaultdict(list)
    unmapped: Counter[str] = Counter()
    source_replacement_paths: list[str] = []
    titles: dict[str, list[str]] = defaultdict(list)
    records: dict[str, tuple[str, TopicMeta]] = {}
    for rel in topics:
        meta = TopicMeta()
        meta.feed((extraction / rel).read_bytes().decode("cp1252", errors="replace"))
        original = html.unescape(meta.title).strip() or Path(rel).stem.replace("_", " ")
        records[rel] = (original, meta)
        titles[original.casefold()].append(rel)
    display_titles: dict[str, str] = {}
    for paths in titles.values():
        if len(paths) == 1:
            display_titles[paths[0]] = records[paths[0]][0]
            continue
        original = records[paths[0]][0]
        used: set[str] = set()
        for rel in paths:
            _, meta = records[rel]
            heading = html.unescape(meta.page_heading).strip()
            candidate = heading if heading and heading.casefold() != original.casefold() else f"{original} ({Path(rel).stem.replace('_', ' ')})"
            display_titles[rel] = candidate
            used.add(candidate.casefold())
        if len(used) != len(paths):
            report["duplicate_titles"].append([original, paths])
    written = 0
    try:
        for rel in topics:
            raw = (extraction / rel).read_bytes()
            source = _normalize_source(raw.decode("cp1252", errors="replace"))
            if "\ufffd" in source:
                source_replacement_paths.append(rel)
            original_title, meta = records[rel]
            title = display_titles[rel]
            try:
                markdown, unknown = run_pandoc(source, tmp)
            except RuntimeError as exc:
                report["pandoc_failures"].append([rel, str(exc)])
                continue
            unmapped.update(unknown)
            for href in _check_links(meta.links, rel, known):
                report["broken_links"].append([rel, href])
            for image_href in meta.images:
                image_path = urldefrag(unquote(image_href))[0]
                if image_path and not (extraction / _norm((Path(rel).parent / image_path).as_posix())).exists():
                    report["broken_images"].append([rel, image_href])
            markdown = re.sub(r"^\s*#\s+.*?\n+", "", markdown, count=1)
            markdown = re.sub(r"^# ", "## ", markdown, flags=re.MULTILINE)
            markdown = _normalize_nested_lists(markdown)
            breadcrumb = crumbs.get(rel) or [part.replace("_", " ") for part in Path(rel).parent.parts]
            if any("chm::" in item or item.endswith(".hhc") for item in breadcrumb):
                breadcrumb = []
            if not crumbs.get(rel):
                report["not_in_toc"].append(rel)
            related = []
            for label, href in meta.related:
                target = _norm((Path(rel).parent / urldefrag(unquote(href))[0]).as_posix())
                if target in known:
                    related.append(f"{label} -> {_relative(rel, target)}")
            page_heading = html.unescape(meta.page_heading).strip()
            if re.sub(r"[^a-z0-9]", "", page_heading.lower()) in {
                re.sub(r"[^a-z0-9]", "", title.lower()),
                re.sub(r"[^a-z0-9]", "", original_title.lower()),
            }:
                page_heading = ""
            fields = {
                "title": title,
                "source_title": original_title,
                "breadcrumb": breadcrumb,
                "source": rel,
                "source_url": (
                    f"{source_url_base.rstrip('/')}/index.htm#t={quote(rel)}"
                    if source_url_base else None
                ),
                "source_hash": source_hash,
                "keywords": list(dict.fromkeys(k.strip() for k in meta.meta.get("rh-index-keywords", "").split(",") if k.strip())),
                "related": related,
                "fw_help_version": version,
                "page_heading": page_heading,
                "type": "index" if "overview" in Path(rel).stem.lower() else "topic",
                "content_hash": "sha256:" + hashlib.sha256(markdown.encode()).hexdigest()[:16],
            }
            trail = " › ".join(breadcrumb[:-1]) if len(breadcrumb) > 1 else ""
            header = f"{frontmatter(fields)}\n\n# {title}\n"
            if trail:
                header += f"\n*{trail}*\n"
            markdown = re.sub(r"^#{1,6} Related Topics\s*$", "## Related topics", markdown, flags=re.MULTILINE)
            markdown = re.sub(r"^#{1,6} Related Internet Sites\s*$", "## Related links", markdown, flags=re.MULTILINE)
            target = destination / Path(rel).with_suffix(".md")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(header + "\n" + markdown.strip() + "\n", encoding="utf-8")
            written += 1
        images = 0
        for image in extraction.rglob("*"):
            if image.is_file() and image.suffix.lower() in IMAGE_EXTS:
                target = destination / image.relative_to(extraction)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(image.read_bytes())
                images += 1
    finally:
        tmp.unlink(missing_ok=True)
    if advisory:
        report["stale_toc_entries"].extend(advisory)
    if unmapped:
        report["unmapped_span_classes"] = [[name, count] for name, count in unmapped.most_common()]
    return {"chm": chm.name, "stem": safe_stem(chm.name), "version": version,
            "toc": toc, "topics": written, "images": images, "report": dict(report),
            "source_replacement_paths": source_replacement_paths,
            "topics_paths": topics}


__all__ = ["convert_chm", "frontmatter", "run_pandoc"]
