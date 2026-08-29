"""Reusable conversion of one extracted CHM into a namespaced Markdown tree."""

from __future__ import annotations

import hashlib
import html
import json
import re
import subprocess
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote, unquote, urldefrag

from chm_extract import _extract_already_locked, extract, validate
from chm_metadata import TopicMeta, parse_toc, safe_stem
from frontmatter import yaml_scalar
from output_fs import export_locks
from source_safety import (
    SourceSafetyError,
    first_link_in_path,
    validate_source_tree,
)

LUA = Path(__file__).with_name("fwhelp.lua")
IMAGE_EXTS = {".gif", ".png", ".jpg", ".jpeg", ".bmp", ".svg", ".ico", ".webp"}
EXTRACTION_MANIFEST = ".chm-extraction-manifest.json"


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

    lines = ["---"]
    for key, value in fields.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {yaml_scalar(item)}" for item in value)
        else:
            lines.append(f"{key}: {yaml_scalar(value)}")
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


_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_URI_ASCII_SEPARATORS = re.compile(r"[\x00-\x20]")


def _uri_kind(raw: str) -> tuple[str, str]:
    value = unquote(urldefrag(raw.strip())[0]).replace("\\", "/")
    canonical = _URI_ASCII_SEPARATORS.sub("", value)
    if not canonical:
        return "fragment", value
    if canonical.startswith("/") or _DRIVE.match(canonical):
        return "path_escape", value
    scheme = _SCHEME.match(canonical)
    if scheme:
        return ("external", value) if scheme.group(0)[:-1].casefold() in {
            "http", "https", "mailto"
        } else ("unsafe_uri", value)
    return "local", value


def _check_links(links: list[str], topic_rel: str, known: set[str]) -> list[str]:
    broken = []
    for href in links:
        kind, value = _uri_kind(href)
        if kind in {"external", "fragment", "unsafe_uri", "path_escape"}:
            continue
        path = value
        if not path or Path(path).suffix.lower() in IMAGE_EXTS:
            continue
        target = _norm((Path(topic_rel).parent / path).as_posix())
        if target.casefold() not in known:
            broken.append(href)
    return broken


def _record_unsafe_uri(report: dict[str, list], rel: str, raw: str) -> None:
    kind, _ = _uri_kind(raw)
    if kind in {"unsafe_uri", "path_escape"}:
        code = f"source_{kind}"
        if not any(
            str(item[0]).casefold() == rel.casefold()
            and str(item[1]).casefold() == raw.casefold()
            for item in report[code]
        ):
            report[code].append([rel, raw])


def _append_reference(report: dict[str, list], code: str, rel: str, raw: str) -> None:
    if not any(
        str(item[0]).casefold() == rel.casefold()
        and str(item[1]).casefold() == raw.casefold()
        for item in report[code]
    ):
        report[code].append([rel, raw])


def _sanitize_markdown_targets(markdown: str, report: dict[str, list], rel: str) -> str:
    """Neutralize unsafe Markdown destinations while preserving safe syntax."""
    opener = re.compile(r"(?<!\\)(?P<image>!)?\[[^\]]*\]\(")
    output: list[str] = []
    cursor = 0
    for match in opener.finditer(markdown):
        output.append(markdown[cursor:match.end()])
        pos, depth = match.end(), 1
        angle = pos < len(markdown) and markdown[pos] == "<"
        while pos < len(markdown):
            char = markdown[pos]
            if angle and char == ">":
                angle = False
            elif not angle and char == "(":
                depth += 1
            elif not angle and char == ")":
                depth -= 1
                if depth == 0:
                    break
            pos += 1
        if depth:
            continue
        inner = markdown[match.end():pos]
        if inner.startswith("<") and ">" in inner:
            target = inner[1:inner.index(">")]
            suffix = inner[inner.index(">") + 1:]
            replacement_target = "<TARGET>"
        else:
            pieces = inner.split(None, 1)
            target = pieces[0] if pieces else ""
            suffix = (" " + pieces[1]) if len(pieces) == 2 else ""
            replacement_target = "TARGET"
        kind, _ = _uri_kind(target)
        if kind in {"unsafe_uri", "path_escape"}:
            report.setdefault(kind, []).append([rel, target])
            replacement = replacement_target.replace("TARGET", "#") + suffix
            output.append(replacement + ")")
        else:
            output.append(inner + ")")
        cursor = pos + 1
    output.append(markdown[cursor:])
    return "".join(output)


def _sanitize_raw_targets(markdown: str, report: dict[str, list], rel: str) -> str:
    pattern = re.compile(
        r"(?P<prefix>\b(?:href|src)\s*=\s*)"
        r"(?:(?P<quote>[\"'])(?P<quoted>.*?)(?P=quote)|(?P<bare>[^\s>]+))",
        re.IGNORECASE,
    )

    def replace(match: re.Match[str]) -> str:
        target = match.group("quoted") if match.group("quote") else match.group("bare")
        kind, _ = _uri_kind(target)
        if kind in {"unsafe_uri", "path_escape"}:
            report.setdefault(kind, []).append([rel, target])
            if match.group("quote"):
                return match.group("prefix") + match.group("quote") + "#" + match.group("quote")
            return match.group("prefix") + "#"
        return match.group(0)

    return pattern.sub(replace, markdown)


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


def _authenticated_manifest(path: Path, chm: Path, source_hash: str) -> bool:
    """Return whether an extraction manifest authenticates this exact CHM."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(loaded, dict):
        return False
    schema = loaded.get("schema")
    return (
        isinstance(schema, int) and not isinstance(schema, bool) and schema == 1
        and loaded.get("source_name") == chm.name
        and loaded.get("source_sha256") == source_hash
        and isinstance(loaded.get("source_sha256"), str)
        and loaded["source_sha256"] == loaded["source_sha256"].lower()
        and len(loaded["source_sha256"]) == 64
        and all(char in "0123456789abcdef" for char in loaded["source_sha256"])
    )


def _write_extraction_manifest(extraction: Path, chm: Path, source_hash: str) -> None:
    extraction.mkdir(parents=True, exist_ok=True)
    (extraction / EXTRACTION_MANIFEST).write_text(
        json.dumps({
            "schema": 1,
            "source_name": chm.name,
            "source_sha256": source_hash,
        }, indent=2) + "\n",
        encoding="utf-8",
    )


def _topic_files(extraction: Path) -> list[Path]:
    return sorted(
        (path for path in extraction.rglob("*")
         if path.is_file() and path.suffix.lower() in {".htm", ".html"}),
        key=lambda path: (
            path.relative_to(extraction).as_posix().casefold(),
            path.relative_to(extraction).as_posix(),
        ),
    )


def _validate_conversion_destination(chm: Path, extraction: Path, destination: Path) -> None:
    """Reject linked or source-overlapping destinations before any write."""
    if (link := first_link_in_path(destination)) is not None:
        raise SourceSafetyError(f"refusing symlink/junction conversion destination: {link}")
    if Path(destination).exists():
        validate_source_tree(destination)
    destination_abs = Path(destination).resolve(strict=False)
    chm_abs = Path(chm).resolve(strict=False)
    extraction_abs = Path(extraction).resolve(strict=False)
    for label, protected in (("CHM", chm_abs), ("extraction", extraction_abs)):
        if destination_abs == protected or destination_abs in protected.parents:
            raise SourceSafetyError(
                f"refusing conversion destination overlapping {label}: {destination}"
            )


def _convert_chm_locked(chm: Path, work_root: Path, destination: Path, *, reuse: bool = False,
                limit: int = 0, source_ref: str = "develop", source_url_base: str | None = None,
                extractor=extract, extract_fn=None) -> dict:
    """Extract and convert one CHM, returning report facts and TOC nodes."""
    chm = Path(chm)
    if extract_fn is not None:
        extractor = extract_fn
    extraction = Path(work_root) / safe_stem(chm.name)
    destination = Path(destination)
    if (link := first_link_in_path(chm)) is not None:
        raise SourceSafetyError(f"refusing symlink/junction CHM path: {link}")
    if (link := first_link_in_path(extraction)) is not None:
        raise SourceSafetyError(f"refusing symlink/junction extraction path: {link}")
    _validate_conversion_destination(chm, extraction, destination)
    source_hash = _sha256(chm)
    advisory: list[str] = []
    manifest = extraction / EXTRACTION_MANIFEST
    if reuse and extraction.exists():
        validate_source_tree(extraction)
    if reuse and _topic_files(extraction) and _authenticated_manifest(manifest, chm, source_hash):
        fatal, advisory = validate(extraction)
        if fatal:
            raise RuntimeError("reused extraction failed validation: " + "; ".join(fatal))
    else:
        default_extractor = extractor is extract
        if extractor is extract:
            # ``convert_chm`` already owns extraction's lock for its entire
            # read/convert lifetime; taking it again would deadlock.
            extractor = _extract_already_locked
        extractor(chm, extraction)
        validate_source_tree(extraction)
        if default_extractor:
            fatal, advisory = validate(extraction)
            if fatal:
                raise RuntimeError(
                    "fresh extraction failed validation: " + "; ".join(fatal)
                )
        else:
            advisory = list(getattr(extractor, "advisory", []))
        # ``extract`` promotes only after its staged extraction passes its
        # checks.  Record identity only after that call has returned.
        _write_extraction_manifest(extraction, chm, source_hash)
    _validate_conversion_destination(chm, extraction, destination)
    hhc = next((path for path in sorted(extraction.rglob("*"), key=lambda item: item.as_posix().casefold())
                if path.is_file() and path.suffix.lower() == ".hhc"), None)
    toc = parse_toc(hhc) if hhc else []
    crumbs = {
        node["href"].casefold(): node["breadcrumb"]
        for node in toc if node["href"]
    }
    version = _version(extraction)
    source_hash = "sha256:" + source_hash
    all_topics = [path.relative_to(extraction).as_posix() for path in _topic_files(extraction)]
    topics = list(all_topics)
    known = {topic.casefold() for topic in topics}
    extraction_files = {
        path.relative_to(extraction).as_posix().casefold()
        for path in extraction.rglob("*") if path.is_file()
    }
    if limit:
        topics = topics[:limit]
    claimed: dict[str, list[str]] = defaultdict(list)
    for rel in all_topics:
        claimed[Path(rel).with_suffix(".md").as_posix().casefold()].append(f"topic:{rel}")
    for image in extraction.rglob("*"):
        if image.is_file() and image.suffix.lower() in IMAGE_EXTS:
            rel = image.relative_to(extraction).as_posix()
            claimed[rel.casefold()].append(f"asset:{rel}")
    collisions = [(dest, paths) for dest, paths in sorted(claimed.items()) if len(paths) > 1]
    if collisions:
        report: dict[str, list] = {"destination_collisions": collisions}
        return {"chm": chm.name, "stem": safe_stem(chm.name), "version": _version(extraction),
                "toc": toc, "topics": 0, "images": 0, "topics_paths": [], "report": report}
    destination.mkdir(parents=True, exist_ok=True)
    # Keep each invocation's scratch file unique even when sibling
    # destinations share a work directory.  The finally block below removes
    # only this converter-owned path.
    tmp = destination.parent / (
        f".{safe_stem(chm.name)}-pandoc-{uuid.uuid4().hex}.html"
    )
    report: dict[str, list] = defaultdict(list)
    unmapped: Counter[str] = Counter()
    unmapped_topics: dict[str, set[str]] = defaultdict(set)
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
            for href in meta.links:
                _record_unsafe_uri(report, rel, href)
            for href in _check_links(meta.links, rel, known):
                _append_reference(report, "broken_links", rel, href)
            for image_href in meta.images:
                _record_unsafe_uri(report, rel, image_href)
                image_kind, image_path = _uri_kind(image_href)
                if image_kind in {"external", "fragment", "unsafe_uri", "path_escape"}:
                    continue
                image_target = _norm((Path(rel).parent / image_path).as_posix())
                if image_path and image_target.casefold() not in extraction_files:
                    _append_reference(report, "broken_images", rel, image_href)
            try:
                markdown, unknown = run_pandoc(source, tmp)
            except RuntimeError as exc:
                report["pandoc_failures"].append([rel, str(exc)])
                continue
            unmapped.update(unknown)
            for class_name in unknown:
                unmapped_topics[class_name].add(rel)
            markdown = re.sub(r"^\s*#\s+.*?\n+", "", markdown, count=1)
            markdown = re.sub(r"^# ", "## ", markdown, flags=re.MULTILINE)
            markdown = _normalize_nested_lists(markdown)
            markdown = _sanitize_markdown_targets(markdown, report, rel)
            markdown = _sanitize_raw_targets(markdown, report, rel)
            breadcrumb = crumbs.get(rel.casefold()) or [part.replace("_", " ") for part in Path(rel).parent.parts]
            if any("chm::" in item or item.endswith(".hhc") for item in breadcrumb):
                breadcrumb = []
            if not crumbs.get(rel.casefold()):
                report["not_in_toc"].append(rel)
            related = []
            for label, href in meta.related:
                target = _norm((Path(rel).parent / urldefrag(unquote(href))[0]).as_posix())
                if target.casefold() in known:
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
            safe_title = yaml_scalar(title)[1:-1]
            trail = " › ".join(breadcrumb[:-1]) if len(breadcrumb) > 1 else ""
            safe_trail = yaml_scalar(trail)[1:-1] if trail else ""
            header = f"{frontmatter(fields)}\n\n# {safe_title}\n"
            if trail:
                header += f"\n*{safe_trail}*\n"
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
    for item in advisory:
        if item.startswith(("source_unsafe_uri:", "source_path_escape:")):
            code, source, raw = item.split(": ", 2)
            if not any(
                str(item[0]).casefold() == source.casefold()
                and str(item[1]).casefold() == raw.casefold()
                for item in report[code]
            ):
                report[code].append([source, raw])
        elif item.startswith("HTML "):
            # HTML missing-target advisories are rechecked above so the
            # converter emits one source_missing_link/image report only.
            continue
        else:
            report["stale_toc_entries"].append(item)
    if unmapped:
        report["unmapped_span_classes"] = [
            [name, count, sorted(unmapped_topics[name], key=str.casefold)]
            for name, count in unmapped.most_common()
        ]
    return {"chm": chm.name, "stem": safe_stem(chm.name), "version": version,
            "toc": toc, "topics": written, "images": images, "report": dict(report),
            "source_replacement_paths": source_replacement_paths,
            "topics_paths": topics}


def convert_chm(chm: Path, work_root: Path, destination: Path, *, reuse: bool = False,
                limit: int = 0, source_ref: str = "develop", source_url_base: str | None = None,
                extractor=extract, extract_fn=None) -> dict:
    """Convert one CHM with deterministic extraction-then-destination locks.

    The extraction/work-root lock is acquired first and held through all
    extraction, validation, source reads, and Pandoc conversion. The
    destination lock is acquired second. This order avoids lock inversion;
    the internal already-locked extraction entry point prevents recursive
    acquisition.
    """
    chm = Path(chm)
    extraction = Path(work_root) / safe_stem(chm.name)
    destination = Path(destination)
    # Perform pure validation first so equal/overlapping targets report the
    # intended source-safety error rather than a duplicate-lock busy error.
    if (link := first_link_in_path(chm)) is not None:
        raise SourceSafetyError(f"refusing symlink/junction CHM path: {link}")
    if (link := first_link_in_path(extraction)) is not None:
        raise SourceSafetyError(f"refusing symlink/junction extraction path: {link}")
    _validate_conversion_destination(chm, extraction, destination)
    with export_locks(extraction, destination):
        # The implementation repeats source and destination validation after
        # lock acquisition, immediately before its first destination write.
        return _convert_chm_locked(
            chm, work_root, destination, reuse=reuse, limit=limit,
            source_ref=source_ref, source_url_base=source_url_base,
            extractor=extractor, extract_fn=extract_fn,
        )


def run_in_private_stage(chm: Path, work_root: Path, destination: Path, *, reuse: bool = False,
                         limit: int = 0, source_ref: str = "develop",
                         source_url_base: str | None = None,
                         extractor=extract, extract_fn=None) -> dict:
    """Convert into a caller-owned private stage while locking extraction.

    The caller must guarantee that ``destination`` is an unshared staging
    path. Such a path needs no destination lock; creating one beside it would
    turn the lockfile into generated content when the enclosing stage is
    promoted.
    """
    chm = Path(chm)
    extraction = Path(work_root) / safe_stem(chm.name)
    destination = Path(destination)
    if (link := first_link_in_path(chm)) is not None:
        raise SourceSafetyError(f"refusing symlink/junction CHM path: {link}")
    if (link := first_link_in_path(extraction)) is not None:
        raise SourceSafetyError(f"refusing symlink/junction extraction path: {link}")
    _validate_conversion_destination(chm, extraction, destination)
    with export_locks(extraction):
        _validate_conversion_destination(chm, extraction, destination)
        return _convert_chm_locked(
            chm, work_root, destination, reuse=reuse, limit=limit,
            source_ref=source_ref, source_url_base=source_url_base,
            extractor=extractor, extract_fn=extract_fn,
        )


__all__ = [
    "convert_chm", "frontmatter", "run_in_private_stage", "run_pandoc", "yaml_scalar",
]
