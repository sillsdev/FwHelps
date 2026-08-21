import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from output_fs import OutputPathError, OutputStaging


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


if __name__ == "__main__":
    unittest.main()
