"""Cross-platform CHM extraction.

Tries, in order:
  1. 7z / 7za / 7zz          (Linux + Windows, best choice for CI)
  2. extract_chmLib          (Linux, chmlib package)
  3. hh.exe -decompile       (Windows only, ships with the OS)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urldefrag

from output_fs import ExportLock, OutputPathError, OutputStaging
from source_safety import first_link_in_path, validate_source_tree


class ExtractError(RuntimeError):
    pass


def _validate_extract_paths(chm: Path, outdir: Path) -> tuple[Path, Path]:
    """Reject extraction destinations that could consume their source."""
    chm = Path(os.path.abspath(os.fspath(Path(chm).expanduser())))
    outdir = Path(os.path.abspath(os.fspath(Path(outdir).expanduser())))
    if (link := first_link_in_path(chm)) is not None:
        raise ExtractError(f"refusing symlink/junction CHM path component: {link}")
    if (link := first_link_in_path(outdir)) is not None:
        raise OutputPathError(f"refusing symlink/junction extraction destination: {link}")
    try:
        source = chm.resolve(strict=True)
    except OSError as exc:
        raise ExtractError(f"no such CHM: {chm}") from exc
    destination = outdir.resolve(strict=False)
    if not source.is_file():
        raise ExtractError(f"no such CHM: {chm}")
    if destination == source or destination in source.parents:
        raise OutputPathError(
            "refusing extraction destination that contains the source CHM: "
            f"source={source}, destination={destination}"
        )
    # Preserve the lexical destination so OutputStaging can independently
    # enforce its own path-chain ownership checks before creating staging.
    return chm, outdir


def _sevenzip(chm: Path, outdir: Path) -> str | None:
    for exe in ("7z", "7za", "7zz"):
        found = shutil.which(exe)
        if not found:
            continue
        subprocess.run(
            [found, "x", "-y", f"-o{outdir}", str(chm)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        return exe
    return None


def _chmlib(chm: Path, outdir: Path) -> str | None:
    found = shutil.which("extract_chmLib")
    if not found:
        return None
    subprocess.run(
        [found, str(chm), str(outdir)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    return "extract_chmLib"


# The deepest path inside FieldWorks_Language_Explorer_Help.chm is ~140 chars.
# hh.exe silently TRUNCATES any output path that exceeds Windows MAX_PATH (260)
# -- no error, no non-zero exit, just a file named "Foo_field_(Extended_Note)"
# with the ".htm" chopped off. Refuse to run rather than corrupt the corpus.
MAX_PATH = 260
ASSUMED_MAX_INTERNAL = 160


def _hh(chm: Path, outdir: Path) -> str | None:
    if os.name != "nt":
        return None
    found = shutil.which("hh") or r"C:\Windows\hh.exe"
    if not Path(found).exists():
        return None

    budget = MAX_PATH - ASSUMED_MAX_INTERNAL
    if len(str(outdir.resolve())) > budget:
        raise ExtractError(
            "hh.exe would silently truncate filenames: output path is "
            f"{len(str(outdir.resolve()))} chars, must be <= {budget}.\n"
            f"  {outdir.resolve()}\n"
            "  Use a shorter --work directory (e.g. C:\\fwhelps-work), or "
            "install 7-Zip, which has no such limit."
        )
    # hh.exe detaches immediately and wants native separators + absolute paths.
    subprocess.run(
        [str(found), "-decompile", str(outdir.resolve()), str(chm.resolve())],
        check=True,
    )
    # It returns before the write finishes; wait for the file count to settle.
    stable, last = 0, -1
    for _ in range(60):
        time.sleep(0.5)
        count = sum(1 for _ in outdir.rglob("*") if _.is_file())
        stable = stable + 1 if count == last and count > 0 else 0
        if stable >= 4:
            break
        last = count
    return "hh.exe"


EXTRACTION_MANIFEST_NAME = ".chm-extraction-manifest.json"


class _ReferenceParser(HTMLParser):
    """Collect CHM sitemap locals and HTML href/src references."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.local: list[str] = []
        self.html: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        if (tag.casefold() == "param"
                and values.get("name", "").casefold() == "local"
                and values.get("value")):
            self.local.append(values["value"])
        for attr in ("href", "src"):
            if values.get(attr):
                self.html.append((attr, values[attr]))


_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_URI_ASCII_SEPARATORS = re.compile(r"[\x00-\x20]")


def _reference_kind(raw: str) -> tuple[str, str]:
    """Return (kind, path), classifying URI safety before filesystem access."""
    value = unquote(urldefrag(raw.strip())[0]).replace("\\", "/")
    canonical = _URI_ASCII_SEPARATORS.sub("", value)
    if not canonical:
        return "fragment", value
    # A leading double slash is a UNC path in CHM content.  It is never a
    # permitted external URL because only explicitly allowed schemes are
    # accepted by the exporter.
    if canonical.startswith(("/", "//")) or _DRIVE.match(canonical):
        return "path_escape", value
    scheme = _SCHEME.match(canonical)
    if scheme:
        return ("external", value) if scheme.group(0)[:-1].casefold() in {
            "http", "https", "mailto"
        } else ("unsafe_uri", value)
    return "local", value


def _prefix_sibling(target: Path, expected: str) -> bool:
    if not target.parent.is_dir():
        return False
    expected_folded = expected.casefold()
    return any(
        sibling.is_file()
        and sibling.name.casefold() != expected_folded
        and expected_folded.startswith(sibling.name.casefold())
        for sibling in target.parent.iterdir()
    )


def validate(outdir: Path) -> tuple[list[str], list[str]]:
    """Check an extraction, separating tool failure from content bugs.

    Returns (fatal, advisory).

    `fatal` means the extractor lost data -- a truncated corpus yields a subtly
    wrong RAG index, which is worse than no index. `advisory` means the CHM
    itself is inconsistent, e.g. the author renamed a topic and left a stale
    TOC entry behind. That is real, but it is the doc author's to fix and must
    not block a build.

    Distinguishing them: hh.exe truncation leaves a sibling whose name is a
    prefix of the expected one ("Discussion_field_(Extended_Note)" for
    "...(Extended_Note).htm"). A genuinely absent topic leaves no such trace.
    """
    fatal, advisory = [], []

    parsers: list[tuple[Path, _ReferenceParser]] = []
    for path in sorted(outdir.rglob("*"), key=lambda p: p.relative_to(outdir).as_posix().casefold()):
        if not path.is_file() or path.name == EXTRACTION_MANIFEST_NAME:
            continue
        if path.suffix.casefold() not in {".hhc", ".htm", ".html"}:
            continue
        parser = _ReferenceParser()
        parser.feed(path.read_text(encoding="cp1252", errors="replace"))
        parsers.append((path, parser))

    if not any(path.suffix.casefold() == ".hhc" for path, _ in parsers):
        fatal.append("no .hhc table of contents found in extraction")
        return fatal, advisory

    seen: set[tuple[str, str, str]] = set()
    for source, parser in parsers:
        references = [("toc", value) for value in parser.local] + parser.html
        for kind, raw in references:
            uri_kind, value = _reference_kind(raw)
            if uri_kind == "fragment" or uri_kind == "external":
                continue
            if uri_kind in {"unsafe_uri", "path_escape"}:
                key = (uri_kind, source.as_posix(), raw)
                if key not in seen:
                    advisory.append(
                        f"source_{uri_kind}: {source.relative_to(outdir).as_posix()}: {raw}"
                    )
                    seen.add(key)
                continue
            target = (source.parent / value).resolve()
            if target != outdir.resolve() and outdir.resolve() not in target.parents:
                advisory.append(
                    f"source_path_escape: {source.relative_to(outdir).as_posix()}: {raw}"
                )
                continue
            if target.exists():
                continue
            if _prefix_sibling(target, target.name):
                message = f"{('TOC target' if kind == 'toc' else 'HTML target')} lost to filename truncation: {value}"
                fatal.append(message)
            elif kind == "toc":
                advisory.append(f"TOC points at a topic that does not exist: {value}")
            else:
                advisory.append(f"HTML {kind} points at a target that does not exist: {value}")

    return fatal, advisory


def _extract_locked(chm: Path, outdir: Path, clean: bool = True, check: bool = True) -> str:
    """Extract `chm` into `outdir`. Returns the name of the tool that worked."""
    chm = Path(chm)
    outdir = Path(outdir)
    chm, outdir = _validate_extract_paths(chm, outdir)
    # Keep failed attempts in private directories beside the destination.  The
    # staging owner guarantees that an existing destination is untouched until
    # a validated extraction is promoted, and same-directory staging makes the
    # final rename safe even when the system temporary directory is another
    # volume.
    errors: list[str] = []
    extract.advisory = []
    for backend in (_sevenzip, _chmlib, _hh):
        try:
            with OutputStaging(outdir) as staging:
                tool = backend(chm, staging.path)
                if not tool:
                    continue
                validate_source_tree(staging.path)
                if not any(
                    path.is_file() and path.suffix.lower() in {".htm", ".html"}
                    for path in staging.rglob("*")
                ):
                    errors.append(f"{tool}: produced no .htm files")
                    continue
                advisory: list[str] = []
                if check:
                    fatal, advisory = validate(staging.path)
                    if fatal:
                        errors.append(
                            f"{tool} produced a corrupt/incomplete extraction "
                            f"({len(fatal)} problems): " + "; ".join(fatal)
                        )
                        continue
                # Content-level inconsistencies ride along for the author
                # report rather than failing the build.
                if clean or not outdir.exists():
                    staging.promote()
                else:
                    # Retain the historical clean=False merge semantics,
                    # but construct the merged tree privately so a copy
                    # failure cannot partially modify the destination.
                    validate_source_tree(outdir)
                    with OutputStaging(outdir) as merged:
                        shutil.copytree(outdir, merged.path, dirs_exist_ok=True)
                        shutil.copytree(staging.path, merged.path, dirs_exist_ok=True)
                        merged.promote()
                extract.advisory = advisory
                return tool
        except subprocess.CalledProcessError as exc:
            errors.append(f"{backend.__name__}: exit {exc.returncode}")
            continue

    raise ExtractError(
        "no working CHM extractor found.\n"
        "  Linux: apt-get install p7zip-full  (or libchm-bin)\n"
        r"  Windows: 7-Zip, or the built-in C:\Windows\hh.exe" "\n"
        + ("  tried: " + "; ".join(errors) if errors else "")
    )


def _extract_already_locked(
    chm: Path, outdir: Path, clean: bool = True, check: bool = True
) -> str:
    """Internal extraction entry point for callers holding ``outdir``'s lock."""
    return _extract_locked(chm, outdir, clean=clean, check=check)


def extract(chm: Path, outdir: Path, clean: bool = True, check: bool = True) -> str:
    """Extract into ``outdir`` while serializing overlapping exporters."""
    chm, outdir = _validate_extract_paths(Path(chm), Path(outdir))
    with ExportLock(outdir):
        # Revalidate after acquiring the lock to close the preflight-to-write
        # path/link window for cooperating invocations.
        chm, outdir = _validate_extract_paths(chm, outdir)
        return _extract_locked(chm, outdir, clean=clean, check=check)


if __name__ == "__main__":
    tool = extract(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"extracted with {tool}")
    for note in getattr(extract, "advisory", []):
        print(f"  advisory: {note}")
