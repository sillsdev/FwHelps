"""Small, repository-independent CHM metadata helpers."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urldefrag


def safe_stem(name: str) -> str:
    """Return a deterministic namespace name for a CHM filename."""
    stem = Path(name).stem
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    return value or "chm"


class TopicMeta(HTMLParser):
    """Facts needed by conversion and corpus provenance checks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self.links: list[str] = []
        self.images: list[str] = []
        self.related: list[tuple[str, str]] = []
        self.page_heading = ""
        self._in_title = False
        self._heading: list[str] | None = None
        self._section = ""
        self._anchor: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = a.get("name") or a.get("http-equiv")
            if key:
                self.meta[key.lower()] = a.get("content", "")
        elif re.fullmatch(r"h[1-6]", tag):
            self._heading = []
        elif tag == "img" and a.get("src"):
            self.images.append(a["src"])
        elif tag == "a" and a.get("href"):
            href = a["href"]
            self.links.append(href)
            self._anchor = (href, [])

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif re.fullmatch(r"h[1-6]", tag) and self._heading is not None:
            text = "".join(self._heading).strip()
            if text and not self.page_heading:
                self.page_heading = text
            if text in {"Related Topics", "Related Internet Sites"}:
                self._section = text
            elif text:
                self._section = ""
            self._heading = None
        elif tag == "a" and self._anchor is not None:
            href, label = self._anchor
            if self._section == "Related Topics":
                self.related.append(("".join(label).strip(), href))
            self._anchor = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._heading is not None:
            self._heading.append(data)
        if self._anchor is not None:
            self._anchor[1].append(data)


class _SitemapParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.entries: list[dict] = []
        self._current: list[tuple[str, str]] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "ul":
            self.depth += 1
        elif tag == "object":
            self._current = []
        elif tag == "param" and self._current is not None:
            self._current.append((a.get("name", "").lower(), a.get("value", "")))

    def handle_endtag(self, tag: str) -> None:
        if tag == "ul":
            self.depth = max(0, self.depth - 1)
        elif tag == "object" and self._current:
            self.entries.append({"depth": self.depth, "params": self._current})
            self._current = None


def parse_toc(path: Path) -> list[dict]:
    parser = _SitemapParser()
    parser.feed(path.read_text(encoding="cp1252", errors="replace"))
    nodes, trail = [], {}
    for entry in parser.entries:
        params = dict(entry["params"])
        title = html.unescape(params.get("name", "")).strip()
        local = unquote(html.unescape(params.get("local", ""))).replace("\\", "/").strip()
        href = urldefrag(local)[0] if local else ""
        depth = entry["depth"]
        trail[depth] = title
        for d in list(trail):
            if d > depth:
                del trail[d]
        nodes.append({
            "title": title,
            "href": href,
            "depth": depth,
            "breadcrumb": [trail[d] for d in sorted(trail) if trail[d]],
            "is_container": not bool(href),
        })
    return nodes


__all__ = ["TopicMeta", "parse_toc", "safe_stem"]
