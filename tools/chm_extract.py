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
from pathlib import Path


class ExtractError(RuntimeError):
    pass


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


KNOWN_EXTS = {
    ".htm", ".html", ".css", ".js", ".gif", ".png", ".jpg", ".jpeg", ".bmp",
    ".hhc", ".hhk", ".glo", ".lng", ".ico", ".svg", ".htt", ".xml", ".txt",
}


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

    # Filenames whose extension got clipped (".ht", ".gi", or none at all).
    suspect = [
        p for p in outdir.rglob("*")
        if p.is_file() and p.suffix.lower() not in KNOWN_EXTS
    ]
    for p in suspect[:20]:
        fatal.append(f"truncated/unknown filename: {p.relative_to(outdir).as_posix()}")
    if len(suspect) > 20:
        fatal.append(f"... and {len(suspect) - 20} more truncated filenames")

    hhc = next(outdir.rglob("*.hhc"), None)
    if hhc is None:
        fatal.append("no .hhc table of contents found in extraction")
        return fatal, advisory

    import html as _html
    from urllib.parse import unquote, urldefrag

    text = hhc.read_text(encoding="cp1252", errors="replace")
    # Values are HTML-escaped in the sitemap ("Texts_&amp;_Words") *and* may be
    # %-encoded; both have to come off before hitting the filesystem.
    targets = {
        urldefrag(unquote(_html.unescape(m)))[0].replace("\\", "/")
        for m in re.findall(r'name="local"\s+value="([^"]+)"', text, re.I)
    }
    for t in sorted(targets):
        if not t or (outdir / t).exists():
            continue
        target = outdir / t
        stem = target.name
        truncated = target.parent.is_dir() and any(
            sib.name != stem and stem.startswith(sib.name)
            for sib in target.parent.iterdir() if sib.is_file()
        )
        if truncated:
            fatal.append(f"TOC target lost to filename truncation: {t}")
        else:
            advisory.append(f"TOC points at a topic that does not exist: {t}")

    return fatal, advisory


def extract(chm: Path, outdir: Path, clean: bool = True, check: bool = True) -> str:
    """Extract `chm` into `outdir`. Returns the name of the tool that worked."""
    chm = Path(chm)
    outdir = Path(outdir)
    if not chm.is_file():
        raise ExtractError(f"no such CHM: {chm}")
    if clean and outdir.exists():
        shutil.rmtree(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    errors = []
    for backend in (_sevenzip, _chmlib, _hh):
        try:
            tool = backend(chm, outdir)
        except subprocess.CalledProcessError as exc:
            errors.append(f"{backend.__name__}: exit {exc.returncode}")
            continue
        if tool:
            if not any(outdir.rglob("*.htm")):
                errors.append(f"{tool}: produced no .htm files")
                continue
            if check:
                fatal, advisory = validate(outdir)
                if fatal:
                    raise ExtractError(
                        f"{tool} produced a corrupt/incomplete extraction "
                        f"({len(fatal)} problems):\n  " + "\n  ".join(fatal)
                    )
                # Content-level inconsistencies ride along for the author
                # report rather than failing the build.
                extract.advisory = advisory
            return tool

    raise ExtractError(
        "no working CHM extractor found.\n"
        "  Linux: apt-get install p7zip-full  (or libchm-bin)\n"
        r"  Windows: 7-Zip, or the built-in C:\Windows\hh.exe" "\n"
        + ("  tried: " + "; ".join(errors) if errors else "")
    )


if __name__ == "__main__":
    tool = extract(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"extracted with {tool}")
    for note in getattr(extract, "advisory", []):
        print(f"  advisory: {note}")
