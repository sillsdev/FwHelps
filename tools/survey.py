"""Survey the FwHelps corpus: what is actually in the CHM and the PDFs.

This is a read-only reconnaissance pass, not the converter. It answers the
questions we need settled before designing the markdown export:
  - how many topics, how big, how deep is the TOC
  - which topics are reachable from the TOC and which are orphans
  - what HTML constructs and CSS classes actually appear (what must map to md)
  - how many internal links resolve, and where the broken ones point
  - which PDFs carry real text vs. scanned images

Usage:  python tools/survey.py [--repo .] [--work DIR] [--json report.json]
"""

from __future__ import annotations

import argparse
import collections
import html
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urldefrag

sys.path.insert(0, str(Path(__file__).parent))
from chm_extract import extract  # noqa: E402


# ---------------------------------------------------------------- sitemap ---

class SitemapParser(HTMLParser):
    """Parses the HTML Help sitemap format shared by .hhc (TOC) and .hhk (index).

    Structure is <ul><li><object><param name= value=>...</object><ul>...</ul></li>.
    Nesting of <ul> gives TOC depth; each <object> holds one or more Name/Local
    pairs.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.entries = []          # {depth, params: [(name, value)]}
        self._current = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "ul":
            self.depth += 1
        elif tag == "object":
            self._current = []
        elif tag == "param" and self._current is not None:
            self._current.append((a.get("name", "").lower(), a.get("value", "")))

    def handle_endtag(self, tag):
        if tag == "ul":
            self.depth = max(0, self.depth - 1)
        elif tag == "object" and self._current is not None:
            if self._current:
                self.entries.append({"depth": self.depth, "params": self._current})
            self._current = None


def parse_toc(path):
    """Return flat TOC nodes with a breadcrumb trail assembled from <ul> depth."""
    p = SitemapParser()
    p.feed(path.read_text(encoding="cp1252", errors="replace"))

    nodes, trail = [], {}
    for e in p.entries:
        params = dict(e["params"])
        name = params.get("name", "").strip()
        local = unquote(params.get("local", "")).replace("\\", "/").strip()
        depth = e["depth"]
        trail[depth] = name
        for d in [d for d in trail if d > depth]:
            del trail[d]
        nodes.append({
            "title": name,
            "href": urldefrag(local)[0] if local else "",
            "depth": depth,
            "breadcrumb": [trail[d] for d in sorted(trail) if trail.get(d)],
            "is_container": not local,
        })
    return nodes


def parse_index(path):
    """Return .hhk keyword entries: {keyword, targets: [(label, href)]}."""
    p = SitemapParser()
    p.feed(path.read_text(encoding="cp1252", errors="replace"))

    out = []
    for e in p.entries:
        keyword, targets, pending = None, [], None
        for k, v in e["params"]:
            if k == "name":
                if keyword is None:
                    keyword = v.strip()
                else:
                    pending = v.strip()
            elif k == "local":
                href = unquote(v).replace("\\", "/").strip()
                targets.append((pending or keyword or "", urldefrag(href)[0]))
                pending = None
        if keyword:
            out.append({"keyword": keyword, "targets": targets})
    return out


# ------------------------------------------------------------------ topic ---

class TopicParser(HTMLParser):
    """Collects structural facts about one topic: tags, classes, links, text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta = {}
        self.tags = collections.Counter()
        self.classes = collections.Counter()
        self.styles = collections.Counter()
        self.links = []
        self.images = []
        self.headings = []
        self.scripts = []
        self.text_parts = []
        self._in_title = False
        self._in_body = False
        self._heading = None
        self._heading_text = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
            return
        if tag == "body":
            self._in_body = True
        if tag == "meta":
            key = a.get("name") or a.get("http-equiv")
            if key:
                self.meta[key.lower()] = a.get("content", "")
            return
        if tag == "script":
            self.scripts.append(a.get("src", "<inline>"))
            return
        if not self._in_body:
            return

        self.tags[tag] += 1
        for cls in a.get("class", "").split():
            self.classes[tag + "." + cls] += 1
        for decl in a.get("style", "").split(";"):
            prop = decl.split(":")[0].strip().lower()
            if prop:
                self.styles[prop] += 1
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag == "img":
            self.images.append(a.get("src", ""))
        if re.fullmatch(r"h[1-6]", tag):
            self._heading = tag
            self._heading_text = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == self._heading:
            self.headings.append([tag, "".join(self._heading_text).strip()])
            self._heading = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._in_body:
            self.text_parts.append(data)
            if self._heading:
                self._heading_text.append(data)

    @property
    def text(self):
        return re.sub(r"\s+", " ", "".join(self.text_parts)).strip()


def survey_topics(root):
    topics = []
    agg = {
        "tags": collections.Counter(),
        "classes": collections.Counter(),
        "styles": collections.Counter(),
        "scripts": collections.Counter(),
        "charsets": collections.Counter(),
        "generators": collections.Counter(),
        "meta_names": collections.Counter(),
    }
    for f in sorted(root.rglob("*.htm")):
        raw = f.read_bytes()
        p = TopicParser()
        p.feed(raw.decode("cp1252", errors="replace"))

        charset = ""
        m = re.search(rb"charset=([\w-]+)", raw, re.I)
        if m:
            charset = m.group(1).decode("ascii", "replace").lower()

        kw = p.meta.get("rh-index-keywords", "")
        topics.append({
            "path": f.relative_to(root).as_posix(),
            "title": html.unescape(p.title).strip(),
            "bytes": len(raw),
            "words": len(p.text.split()),
            "keywords": [k.strip() for k in kw.split(",") if k.strip()],
            "headings": p.headings,
            "links": p.links,
            "images": p.images,
            "tables": p.tags.get("table", 0),
            "charset": charset,
            "non_ascii_bytes": sum(1 for b in raw if b > 0x7F),
        })
        agg["tags"].update(p.tags)
        agg["classes"].update(p.classes)
        agg["styles"].update(p.styles)
        agg["scripts"].update(p.scripts)
        agg["meta_names"].update(p.meta.keys())
        agg["charsets"][charset or "(none)"] += 1
        agg["generators"][p.meta.get("generator", "(none)")] += 1
    return topics, agg


def classify_links(topics, root):
    kinds = collections.Counter()
    broken = []
    external = collections.Counter()
    for t in topics:
        base = (root / t["path"]).parent
        for href in t["links"]:
            low = href.lower()
            if low.startswith(("http://", "https://")):
                kinds["external"] += 1
                external[re.sub(r"^https?://([^/]+).*", r"\1", href, flags=re.I)] += 1
            elif low.startswith("mailto:"):
                kinds["mailto"] += 1
            elif low.startswith(("javascript:", "#")):
                kinds["anchor/script"] += 1
            else:
                kinds["internal"] += 1
                target = urldefrag(unquote(href))[0]
                if target and not (base / target).exists():
                    broken.append([t["path"], href])
    return {"kinds": dict(kinds), "broken": broken, "external_hosts": dict(external)}


# -------------------------------------------------------------------- pdf ---

def survey_pdfs(repo):
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return [{"error": "PyMuPDF (fitz) not installed; skipping PDF survey"}]

    out = []
    for f in sorted(repo.rglob("*.pdf")):
        if ".git" in f.parts:
            continue
        rec = {"path": f.relative_to(repo).as_posix(), "bytes": f.stat().st_size}
        try:
            with fitz.open(f) as doc:
                rec["pages"] = doc.page_count
                rec["toc_entries"] = len(doc.get_toc())
                rec["encrypted"] = doc.is_encrypted
                sample = list(range(min(doc.page_count, 12)))
                chars = images = 0
                for i in sample:
                    page = doc[i]
                    chars += len(page.get_text("text").strip())
                    images += len(page.get_images(full=True))
                rec["chars_per_page"] = round(chars / max(1, len(sample)))
                rec["images_per_page"] = round(images / max(1, len(sample)), 1)
                rec["text_layer"] = (
                    "yes" if rec["chars_per_page"] > 200
                    else "sparse" if rec["chars_per_page"] > 20
                    else "NONE (scanned?)"
                )
                rec["metadata_title"] = (doc.metadata or {}).get("title") or ""
        except Exception as exc:
            rec["error"] = "{}: {}".format(type(exc).__name__, exc)
        out.append(rec)
    return out


# ------------------------------------------------------------------- main ---

W = 78


def rule(title=""):
    print("\n" + ((("-- " + title + " ").ljust(W, "-")) if title else "-" * W))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".", type=Path)
    ap.add_argument("--work", default=None, type=Path,
                    help="scratch dir for CHM extraction (default: <repo>/.chm-work)")
    ap.add_argument("--json", default=None, type=Path)
    ap.add_argument("--reuse", action="store_true",
                    help="reuse an existing extraction instead of re-extracting")
    args = ap.parse_args()

    repo = args.repo.resolve()
    work = (args.work or repo / ".chm-work").resolve()
    chm = repo / "FieldWorks_Language_Explorer_Help.chm"

    if args.reuse and any(work.rglob("*.htm")):
        tool = "(reused existing extraction)"
    else:
        tool = extract(chm, work)
    print("CHM extracted with: {}\n  -> {}".format(tool, work))

    hhc = next(work.rglob("*.hhc"), None)
    hhk = next(work.rglob("*.hhk"), None)
    toc = parse_toc(hhc) if hhc else []
    idx = parse_index(hhk) if hhk else []

    topics, agg = survey_topics(work)
    links = classify_links(topics, work)
    pdfs = survey_pdfs(repo)

    by_path = {t["path"]: t for t in topics}
    toc_hrefs = {n["href"] for n in toc if n["href"]}
    orphans = sorted(set(by_path) - toc_hrefs)
    dangling = sorted(h for h in toc_hrefs if h not in by_path)

    version = ""
    if hhk:
        m = re.search(r"_(\d+\.\d+)\.hhk$", hhk.name)
        version = m.group(1) if m else ""

    words = sorted(t["words"] for t in topics)

    rule("CORPUS")
    print("  help version (from .hhk)    {}".format(version or "?"))
    print("  topics (.htm)               {}".format(len(topics)))
    print("  total body words            {:,}".format(sum(words)))
    print("  total bytes                 {:,}".format(sum(t["bytes"] for t in topics)))
    print("  words/topic  min/med/max    {} / {} / {}".format(
        words[0], words[len(words) // 2], words[-1]))
    biggest = max(topics, key=lambda t: t["words"])
    print("  largest topic               {} words  {}".format(biggest["words"], biggest["path"]))
    print("  topics under 30 words       {}".format(sum(1 for w in words if w < 30)))
    print("  topics over 1000 words      {}".format(sum(1 for w in words if w > 1000)))
    print("  distinct images referenced  {}".format(len({i for t in topics for i in t["images"]})))
    print("  topics containing tables    {}".format(sum(1 for t in topics if t["tables"])))

    rule("TOC (.hhc)")
    print("  nodes                       {}  ({} containers, {} topic links)".format(
        len(toc), sum(1 for n in toc if n["is_container"]), len(toc_hrefs)))
    print("  max depth                   {}".format(max((n["depth"] for n in toc), default=0)))
    print("  topics NOT in TOC           {}".format(len(orphans)))
    print("  TOC links with no file      {}".format(len(dangling)))
    for o in orphans[:10]:
        print("      orphan:   {}".format(o))
    for d in dangling[:10]:
        print("      dangling: {}".format(d))

    rule("INDEX (.hhk)")
    print("  keywords                    {}".format(len(idx)))
    print("  keyword -> topic refs       {}".format(sum(len(e["targets"]) for e in idx)))
    print("  topics w/ rh-index-keywords {} / {}".format(
        sum(1 for t in topics if t["keywords"]), len(topics)))
    allkw = collections.Counter(k for t in topics for k in t["keywords"])
    print("  distinct meta keywords      {}".format(len(allkw)))
    print("  most common: " + ", ".join("{}({})".format(k, n) for k, n in allkw.most_common(6)))

    rule("HTML CONSTRUCTS  (what the converter must handle)")
    print("  tags:     " + ", ".join("{}:{}".format(t, n) for t, n in agg["tags"].most_common(24)))
    print("  classes:  " + ", ".join("{}:{}".format(c, n) for c, n in agg["classes"].most_common(20)))
    print("  inline styles: " + ", ".join("{}:{}".format(s, n) for s, n in agg["styles"].most_common(10)))
    print("  charsets: " + ", ".join("{}:{}".format(c, n) for c, n in agg["charsets"].most_common()))
    print("  generators: " + ", ".join("{}:{}".format(g, n) for g, n in agg["generators"].most_common(3)))
    print("  scripts:  " + ", ".join("{}:{}".format(s, n) for s, n in agg["scripts"].most_common(5)))
    print("  meta names: " + ", ".join("{}:{}".format(m, n) for m, n in agg["meta_names"].most_common(14)))
    print("  topics with non-ASCII bytes: {}".format(sum(1 for t in topics if t["non_ascii_bytes"])))
    print("  total non-ASCII bytes:       {:,}".format(sum(t["non_ascii_bytes"] for t in topics)))

    rule("LINKS")
    for k, v in sorted(links["kinds"].items(), key=lambda kv: -kv[1]):
        print("  {:<16} {}".format(k, v))
    print("  BROKEN internal  {}".format(len(links["broken"])))
    for src, href in links["broken"][:10]:
        print("      {}  ->  {}".format(src, href))
    print("  external hosts:  " + ", ".join(
        "{}({})".format(h, n)
        for h, n in sorted(links["external_hosts"].items(), key=lambda kv: -kv[1])[:8]))

    rule("PDFs")
    print("  {:>5} {:>6} {:>7}  {:<14} {}".format("pages", "ch/pg", "img/pg", "text", "path"))
    for p in pdfs:
        if "pages" not in p:
            print("  {:>5} {:>6} {:>7}  {:<14} {}  {}".format(
                "?", "?", "?", "ERROR", p.get("path", ""), p.get("error", "")))
            continue
        print("  {:>5} {:>6} {:>7}  {:<14} {}".format(
            p["pages"], p["chars_per_page"], p["images_per_page"], p["text_layer"], p["path"]))
    ok = [p for p in pdfs if p.get("pages")]
    if ok:
        print("  total pages {}, with PDF bookmarks: {}/{}".format(
            sum(p["pages"] for p in ok),
            sum(1 for p in ok if p.get("toc_entries")), len(ok)))

    rule()
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({
            "version": version,
            "toc": toc,
            "index": idx,
            "topics": topics,
            "orphans": orphans,
            "dangling": dangling,
            "links": links,
            "pdfs": pdfs,
            "aggregate": {k: dict(v) for k, v in agg.items()},
        }, indent=1), encoding="utf-8")
        print("full report -> {}".format(args.json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
