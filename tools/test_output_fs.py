import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from output_fs import (
    ExportBusyError,
    ExportLock,
    OutputPathError,
    OutputStaging,
    export_locks,
)


class OutputStagingSafetyTests(unittest.TestCase):
    def test_rejects_filesystem_root_before_touching_it(self):
        with self.assertRaises(OutputPathError):
            OutputStaging(Path(Path.cwd().anchor))

    def test_uses_destination_parent_for_owned_stage_when_work_is_omitted(self):
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "output"
            with OutputStaging(destination) as staged:
                self.assertEqual(destination.parent, staged.path.parent)
                (staged / "fresh.txt").write_text("fresh", encoding="utf-8")
                staged.promote()
            self.assertEqual("fresh", (destination / "fresh.txt").read_text(encoding="utf-8"))

    def test_implicit_sibling_stage_is_allowed_under_protected_repo_root(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw) / "repo"
            repo.mkdir()
            destination = repo / "out"
            with OutputStaging(destination, repo_root=repo, source_root=repo) as staged:
                self.assertEqual(repo, staged.path.parent)
                (staged / "fresh.txt").write_text("fresh", encoding="utf-8")
                staged.promote()
            self.assertEqual("fresh", (destination / "fresh.txt").read_text(encoding="utf-8"))

    def test_explicit_repo_root_work_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            with self.assertRaises(OutputPathError):
                OutputStaging(root / "out", work_dir=repo, repo_root=repo, source_root=repo)

    def test_rejects_symlink_destination_and_symlink_ancestor_before_resolution(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            real = root / "real"
            real.mkdir()
            link = root / "link"
            try:
                link.symlink_to(real, target_is_directory=True)
            except (OSError, NotImplementedError):
                if os.name == "nt":
                    junction = subprocess.run(
                        ["cmd.exe", "/c", "mklink", "/J", str(link), str(real)],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if junction.returncode != 0:
                        self.skipTest("directory links are unavailable")
                else:
                    self.skipTest("directory symlinks are unavailable")

            try:
                for destination in (link, link / "generated"):
                    with self.subTest(destination=destination), self.assertRaises(OutputPathError):
                        OutputStaging(destination)
                self.assertTrue(real.is_dir())
            finally:
                if link.is_symlink():
                    link.unlink()
                elif link.exists():
                    link.rmdir()

    def test_rejects_repository_and_source_roots_before_removal(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            source = root / "source"
            repo.mkdir()
            source.mkdir()
            for forbidden in (repo, source):
                with self.subTest(forbidden=forbidden):
                    marker = forbidden / "keep.txt"
                    marker.write_text("keep", encoding="utf-8")
                    with self.assertRaises(OutputPathError):
                        OutputStaging(forbidden, repo_root=repo, source_root=source)
                    self.assertTrue(marker.exists())

    def test_rejects_overlapping_work_and_output_paths(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            work = root / "work"
            output = root / "output"
            work.mkdir()
            output.mkdir()
            with self.assertRaises(OutputPathError):
                OutputStaging(output / "nested", work_dir=output, repo_root=root / "repo")
            with self.assertRaises(OutputPathError):
                OutputStaging(output, work_dir=output / "nested", repo_root=root / "repo")

    def test_promote_replaces_destination_and_failed_context_preserves_it(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            work = root / "work"
            destination = root / "output"
            work.mkdir()
            destination.mkdir()
            (destination / "stale.txt").write_text("stale", encoding="utf-8")

            with OutputStaging(destination, work_dir=work, repo_root=root / "repo") as staged:
                self.assertEqual([], list(staged.iterdir()))
                (staged / "fresh.txt").write_text("fresh", encoding="utf-8")
                staged.promote()

            self.assertEqual("fresh", (destination / "fresh.txt").read_text(encoding="utf-8"))
            self.assertFalse((destination / "stale.txt").exists())
            self.assertFalse(any(p.name.startswith(".output-stage-") for p in work.iterdir()))

            destination.joinpath("keep.txt").write_text("keep", encoding="utf-8")
            with self.assertRaises(RuntimeError), OutputStaging(
                destination, work_dir=work, repo_root=root / "repo"
            ) as staged:
                (staged / "discarded.txt").write_text("discard", encoding="utf-8")
                raise RuntimeError("build failed")
            self.assertEqual("keep", (destination / "keep.txt").read_text(encoding="utf-8"))
            self.assertFalse((destination / "discarded.txt").exists())
            self.assertFalse(any(p.name.startswith(".output-stage-") for p in work.iterdir()))


class ExportLockTests(unittest.TestCase):
    def test_lock_rejects_another_invocation_for_same_destination(self):
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "output"
            with ExportLock(destination) as held:
                with self.assertRaisesRegex(ExportBusyError, "export destination is busy"):
                    ExportLock(destination).acquire()
                self.assertEqual(held.lock_path, ExportLock(destination).lock_path)

    def test_different_destinations_do_not_contend(self):
        with tempfile.TemporaryDirectory() as raw:
            first = Path(raw) / "first"
            second = Path(raw) / "second"
            with ExportLock(first), ExportLock(second):
                pass

    def test_exception_releases_lock(self):
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "output"
            with self.assertRaises(RuntimeError), ExportLock(destination):
                raise RuntimeError("failure")
            with ExportLock(destination):
                pass

    def test_stale_lockfile_does_not_block_acquisition(self):
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "output"
            lock = ExportLock(destination)
            lock.lock_path.write_text("stale owner metadata\n", encoding="utf-8")
            with ExportLock(destination):
                pass
            self.assertEqual("stale owner metadata\n", lock.lock_path.read_text(encoding="utf-8"))

    def test_linked_lock_parent_is_rejected_before_open(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            real = root / "real"
            real.mkdir()
            linked = root / "linked"
            try:
                linked.symlink_to(real, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory links are unavailable")
            with self.assertRaises(OutputPathError):
                ExportLock(linked / "output").acquire()

    def test_export_locks_deduplicates_targets_and_releases_partial_acquisition(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first, second = root / "first", root / "second"
            with (
                ExportLock(second),
                self.assertRaises(ExportBusyError),
                export_locks(first, second, first),
            ):
                pass
            with export_locks(first, first) as locks:
                self.assertEqual(1, len(locks))
            with ExportLock(first):
                pass

    def test_export_locks_uses_stable_order_for_reversed_targets(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first, second = root / "first", root / "second"
            expected = sorted(
                (
                    os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(first)))),
                    os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(second)))),
                )
            )
            with export_locks(second, first) as locks:
                actual = [
                    os.path.normcase(os.path.normpath(os.fspath(lock.destination)))
                    for lock in locks
                ]
                self.assertEqual(expected, actual)

    def test_reversed_contention_releases_any_lock_acquired_before_busy_target(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first, second = root / "first", root / "second"
            with (
                ExportLock(first),
                self.assertRaises(ExportBusyError),
                export_locks(second, first),
            ):
                pass
            with ExportLock(second):
                pass


if __name__ == "__main__":
    unittest.main()
