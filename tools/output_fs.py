"""Owned staging and conservative filesystem promotion for generated trees.

The public surface deliberately has no general-purpose delete operation.  A
``OutputStaging`` instance may remove only its own temporary tree and the
destination tree that it validated before promotion.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Self


class OutputPathError(ValueError):
    """A requested generated path is unsafe or internally inconsistent."""


def _lexical_absolute(path: Path | str) -> Path:
    """Make an absolute, normalized path without following links."""
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _absolute(path: Path | str) -> Path:
    return _lexical_absolute(path).resolve(strict=False)


def _first_link(path: Path) -> Path | None:
    """Return the first symlink/junction in a lexical path, if any."""
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        is_junction = getattr(current, "is_junction", lambda: False)
        if current.is_symlink() or is_junction():
            return current
    return None


def _is_root(path: Path) -> bool:
    return path == Path(path.anchor)


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def validate_output_paths(
    destination: Path | str,
    *,
    work_dir: Path | str | None = None,
    repo_root: Path | str | None = None,
    source_root: Path | str | None = None,
) -> tuple[Path, Path]:
    """Validate generated paths before staging or recursive removal.

    Repository and source roots are protected exact paths.  Their children are
    valid outputs (the normal CLI writes ``repo/out``), while trying to replace
    either root itself is rejected.  ``work_dir`` and ``destination`` may not
    overlap because either relationship could make a promotion consume its
    own input tree.
    """

    destination_lexical = _lexical_absolute(destination)
    destination_path = destination_lexical.resolve(strict=False)
    explicit_work = work_dir is not None
    work_lexical = _lexical_absolute(work_dir) if explicit_work else destination_lexical.parent
    work_path = work_lexical.resolve(strict=False)
    protected = {
        label: (_lexical_absolute(value), _absolute(value))
        for label, value in (("repository", repo_root), ("source", source_root))
        if value is not None
    }

    for label, lexical, path in (
        ("destination", destination_lexical, destination_path),
        ("work", work_lexical, work_path),
    ):
        if _is_root(path):
            raise OutputPathError(f"refusing filesystem root as {label}: {path}")
        if (link := _first_link(lexical)) is not None:
            raise OutputPathError(f"refusing symlink/junction in {label}: {link}")

    for label, (protected_lexical, protected_path) in protected.items():
        if _is_root(protected_path):
            raise OutputPathError(f"refusing filesystem root as {label} root: {protected_path}")
        if (link := _first_link(protected_lexical)) is not None:
            raise OutputPathError(f"refusing symlink/junction in {label} root: {link}")
        if destination_path == protected_path:
            raise OutputPathError(f"destination must not replace {label} root: {destination_path}")
        if explicit_work and work_path == protected_path:
            raise OutputPathError(f"work directory must not be {label} root: {work_path}")

    if explicit_work and _overlaps(destination_path, work_path):
        raise OutputPathError(
            "work and output paths must not overlap: "
            f"work={work_path}, output={destination_path}"
        )
    return destination_path, work_path


class OutputStaging:
    """Own a temporary generated tree and promote it as one destination.

    ``with OutputStaging(...) as stage`` yields this object; use ``stage.path``
    or path-like operations to populate it, then call ``stage.promote()`` only
    after the caller's validation succeeds.  Exiting without promotion removes
    only the temporary tree and leaves an existing destination untouched.
    """

    _STAGE_PREFIX = ".output-stage-"
    _BACKUP_PREFIX = ".output-backup-"

    def __init__(
        self,
        destination: Path | str,
        *,
        work_dir: Path | str | None = None,
        repo_root: Path | str | None = None,
        source_root: Path | str | None = None,
    ) -> None:
        self.destination, self.work_dir = validate_output_paths(
            destination,
            work_dir=work_dir,
            repo_root=repo_root,
            source_root=source_root,
        )
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.path = Path(tempfile.mkdtemp(prefix=self._STAGE_PREFIX, dir=self.work_dir))
        self._promoted = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if not self._promoted:
            self._remove_owned(self.path, self._STAGE_PREFIX)
        return False

    def __fspath__(self) -> str:
        return os.fspath(self.path)

    def __truediv__(self, child: str) -> Path:
        return self.path / child

    def iterdir(self):
        return self.path.iterdir()

    def rglob(self, pattern: str):
        return self.path.rglob(pattern)

    def promote(self) -> Path:
        """Replace the validated destination with the staged tree."""
        if self._promoted:
            raise RuntimeError("staging tree has already been promoted")
        if not self.path.is_dir() or self.path.parent != self.work_dir:
            raise OutputPathError("staging tree is no longer owned by this instance")

        self.destination.parent.mkdir(parents=True, exist_ok=True)
        backup: Path | None = None
        if self.destination.exists() or self.destination.is_symlink():
            if self.destination.is_symlink():
                raise OutputPathError(f"refusing symlink destination: {self.destination}")
            backup = self.destination.parent / f"{self._BACKUP_PREFIX}{uuid.uuid4().hex}"
            os.replace(self.destination, backup)
        try:
            os.replace(self.path, self.destination)
        except Exception:
            if backup is not None and not self.destination.exists():
                os.replace(backup, self.destination)
            raise
        if backup is not None:
            self._remove_owned(backup, self._BACKUP_PREFIX)
        self._promoted = True
        return self.destination

    @staticmethod
    def _remove_owned(path: Path, prefix: str) -> None:
        """Remove a path only when it is a child with our ownership prefix."""
        if path.name.startswith(prefix) and path.parent != path:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink()


__all__ = ["OutputPathError", "OutputStaging", "validate_output_paths"]
