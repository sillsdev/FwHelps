"""Safe, deterministic discovery of repository source files."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class SourceSafetyError(ValueError):
    """A source root or source input violates the repository boundary."""


def _absolute_lexical(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _is_link(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", lambda: False)
    return path.is_symlink() or is_junction()


def _first_link(path: Path) -> Path | None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if _is_link(current):
            return current
    return None


def first_link_in_path(path: Path | str) -> Path | None:
    """Return the first symlink/junction in a lexical path chain."""
    return _first_link(_absolute_lexical(path))


def validate_source_tree(root: Path) -> Path:
    """Validate a lexical tree without following links or escaping root."""
    lexical_root = _absolute_lexical(root)
    if (link := first_link_in_path(lexical_root)) is not None:
        raise SourceSafetyError(f"refusing symlink/junction tree root: {link}")
    try:
        resolved_root = lexical_root.resolve(strict=True)
    except OSError as exc:
        raise SourceSafetyError(f"source tree does not exist: {root}") from exc
    if not resolved_root.is_dir():
        raise SourceSafetyError(f"source tree is not a directory: {root}")

    pending = [lexical_root]
    while pending:
        current = pending.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda item: (item.name.casefold(), item.name))
        except OSError as exc:
            raise SourceSafetyError(f"cannot inspect source tree: {current}") from exc
        for entry in entries:
            if (link := first_link_in_path(entry)) is not None:
                raise SourceSafetyError(f"refusing symlink/junction in source tree: {link}")
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as exc:
                raise SourceSafetyError(f"cannot inspect source tree entry: {entry}") from exc
            if stat.S_ISDIR(mode):
                _resolved_inside(entry, resolved_root)
                pending.append(entry)
            elif stat.S_ISREG(mode):
                _resolved_inside(entry, resolved_root)
    return lexical_root


def _resolved_inside(path: Path, root: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SourceSafetyError(f"cannot resolve source path: {path}") from exc
    if resolved != root and root not in resolved.parents:
        raise SourceSafetyError(f"source path resolves outside repository root: {path}")
    return resolved


def discover_source_files(
    root: Path,
    *,
    suffixes: set[str],
    recursive: bool,
    exclude_dirs: set[str] | None = None,
) -> list[Path]:
    """Discover regular source files without following links.

    Candidate files and directories that will be traversed are checked for
    links and for a resolved path outside the resolved root. Irrelevant files
    and excluded directories are ignored before those checks, so an unrelated
    link cannot abort discovery or become an input boundary.
    """
    output_root = (
        Path(os.path.normpath(os.fspath(Path(root).expanduser())))
        if not Path(root).expanduser().is_absolute()
        else _absolute_lexical(root)
    )
    lexical_root = _absolute_lexical(root)
    if _first_link(lexical_root) is not None:
        raise SourceSafetyError(f"refusing symlink/junction source root: {root}")
    try:
        resolved_root = lexical_root.resolve(strict=True)
    except OSError as exc:
        raise SourceSafetyError(f"source root does not exist: {root}") from exc
    if not resolved_root.is_dir():
        raise SourceSafetyError(f"source root is not a directory: {root}")

    wanted = {suffix.casefold() if suffix.startswith(".") else f".{suffix.casefold()}"
              for suffix in suffixes}
    # Repository metadata is never a source tree, even when callers do not
    # provide an explicit exclusion set.  Additional exclusions are additive;
    # callers cannot accidentally opt .git back into traversal.
    excluded = {".git"} | {name.casefold() for name in (exclude_dirs or set())}
    pending = [lexical_root]
    discovered: list[tuple[Path, Path]] = []
    while pending:
        current = pending.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda item: (item.name.casefold(), item.name))
        except OSError as exc:
            raise SourceSafetyError(f"cannot inspect source directory: {current}") from exc
        for entry in entries:
            if entry.name.casefold() in excluded:
                continue
            if _is_link(entry):
                if entry.suffix.casefold() in wanted and not entry.is_dir():
                    raise SourceSafetyError(f"refusing symlink/junction source input: {entry}")
                continue
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                if recursive:
                    _resolved_inside(entry, resolved_root)
                    pending.append(entry)
                continue
            if not stat.S_ISREG(mode):
                continue
            if entry.suffix.casefold() not in wanted:
                continue
            _resolved_inside(entry, resolved_root)
            discovered.append((entry.relative_to(lexical_root), output_root / entry.relative_to(lexical_root)))

    return sorted(
        (path for _, path in discovered),
        key=lambda item: (
            item.relative_to(output_root).as_posix().casefold(),
            item.relative_to(output_root).as_posix(),
        ),
    )


__all__ = [
    "SourceSafetyError",
    "discover_source_files",
    "first_link_in_path",
    "validate_source_tree",
]
