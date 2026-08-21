"""Validate the files actually emitted by the portable Markdown exporter."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urldefrag

from reporting import Issue

_EXTERNAL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)
_FENCE = re.compile(r"^\s*(```|~~~)")


class _RawHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.targets: list[tuple[str, str]] = []
        self.tags: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        self.tags.append(tag.lower())
        values = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "a" and values.get("href"):
            self.targets.append(("href", values["href"]))
        if tag.lower() == "img" and values.get("src"):
            self.targets.append(("src", values["src"]))


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    fields = {}
    for line in text[4:end].splitlines():
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip().strip('"')
    return fields


def _target_path(raw: str) -> str:
    return urldefrag(unquote(raw.replace("\\", "/")))[0]


def _is_external(raw: str) -> bool:
    return not raw or raw.startswith("#") or _EXTERNAL.match(raw) is not None


def _resolve(root: Path, source: Path, raw: str) -> Path | None:
    path = _target_path(raw)
    if _is_external(path) or not path:
        return None
    # Absolute paths are never local corpus references.
    if path.startswith(("/", "\\")):
        return None
    return (source.parent / path).resolve()


def _inside_root(root: Path, candidate: Path) -> bool:
    return candidate == root or root in candidate.parents


def _markdown_targets(text: str):
    """Yield (is_image, target) while balancing parentheses in destinations."""
    start = re.compile(r"(?<!\\)(?P<image>!)?\[[^\]]*\]\(")
    for match in start.finditer(text):
        cursor, depth = match.end(), 1
        angle = cursor < len(text) and text[cursor] == "<"
        while cursor < len(text):
            char = text[cursor]
            if angle and char == ">":
                angle = False
            elif not angle and char == "(":
                depth += 1
            elif not angle and char == ")":
                depth -= 1
                if depth == 0:
                    break
            cursor += 1
        if depth:
            continue
        value = text[match.end():cursor].strip()
        if value.startswith("<") and ">" in value:
            value = value[1:value.find(">")]
        else:
            value = value.split(None, 1)[0] if value else ""
        yield bool(match.group("image")), value


def _without_fenced(text: str) -> str:
    lines, fenced = [], False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            lines.append(line)
    return "\n".join(lines)


def validate_corpus(root: Path, *, advisory_links: set[tuple[str, str]] | None = None,
                    source_replacement_paths: set[str] | None = None,
                    source_links: set[tuple[str, str]] | None = None,
                    advisory_images: set[tuple[str, str]] | None = None) -> list[Issue]:
    """Return issues for a corpus tree; never mutate the emitted files."""
    root = Path(root).resolve()
    advisory_links = advisory_links or source_links or set()
    advisory_images = advisory_images or set()
    source_replacement_paths = source_replacement_paths or set()
    issues: list[Issue] = []
    markdown = sorted(root.rglob("*.md"))
    titles: dict[str, list[str]] = {}
    for path in markdown:
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        body = text
        if text.startswith("---") and "\n---" in text[3:]:
            body = text[text.find("\n---", 3) + 4:]
        link_body = _without_fenced(body)
        h1s = []
        fenced = False
        for line in body.splitlines():
            if _FENCE.match(line):
                fenced = not fenced
                continue
            if fenced:
                continue
            heading = re.match(r"^\s*(#{1,6})\s+(.+?)\s*#*\s*$", line)
            if heading and len(heading.group(1)) == 1:
                h1s.append(heading.group(2).strip())
        if len(h1s) != 1:
            issues.append(Issue("one_h1", f"expected one H1, found {len(h1s)}", rel, True))
        if "\ufffd" in text:
            source = rel in source_replacement_paths
            issues.append(Issue("replacement_character", "contains U+FFFD", rel,
                                not source, "source" if source else "exporter"))
        if re.search(r"(?m)^\s*-\s+-\s+", link_body):
            issues.append(Issue("malformed_list", "literal '- -' list marker", rel, True))
        if h1s:
            titles.setdefault(h1s[0].casefold(), []).append(rel)

        # Parse Markdown links separately so image links do not produce a
        # second ordinary-link finding.
        for is_image, raw in _markdown_targets(link_body):
            target = _resolve(root, path, raw)
            if target is not None:
                inside = _inside_root(root, target)
                code = "missing_image" if is_image else "missing_link"
                if not inside or not target.exists():
                    # Source-authored missing targets are visible but advisory;
                    # an escape is always an exporter safety error.
                    advisory = inside and (
                        (not is_image and (rel, raw) in advisory_links)
                        or (is_image and (rel, raw) in advisory_images)
                    )
                    issues.append(Issue(
                        code,
                        f"target {'escapes corpus root' if not inside else 'does not exist'}: {raw}",
                        rel, fatal=not advisory,
                        provenance="source" if advisory else "exporter",
                    ))

        raw_html = _RawHTML()
        raw_html.feed(link_body)
        if any(tag not in {"a", "img"} for tag in raw_html.tags):
            issues.append(Issue(
                "raw_html", f"raw HTML tags: {', '.join(sorted(set(raw_html.tags)))}",
                rel, False, "source",
            ))
        for kind, raw in raw_html.targets:
            target = _resolve(root, path, raw)
            if target is not None and (not target.exists() or not _inside_root(root, target)):
                code = "missing_image" if kind == "src" else "missing_link"
                inside = _inside_root(root, target)
                advisory = inside and (
                    (kind == "href" and (rel, raw) in advisory_links)
                    or (kind == "src" and (rel, raw) in advisory_images)
                )
                issues.append(Issue(
                    code,
                    f"raw HTML target {'escapes corpus root' if not inside else 'does not exist'}: {raw}",
                    rel, not advisory, "source" if advisory else "exporter",
                ))

    for title, paths in sorted(titles.items()):
        if len(paths) > 1:
            issues.append(Issue(
                "duplicate_title", f"display title {title!r}: {', '.join(paths)}",
                paths[0], False, "source", paths,
            ))
    return issues


validate_emitted_corpus = validate_corpus

__all__ = ["validate_corpus", "validate_emitted_corpus"]
