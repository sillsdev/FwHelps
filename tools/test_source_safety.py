import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from source_safety import SourceSafetyError, discover_source_files, validate_source_tree


class SourceSafetyTests(unittest.TestCase):
    def test_discovers_regular_files_in_deterministic_casefolded_order(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "z.PDF").write_bytes(b"")
            (root / "A.pdf").write_bytes(b"")
            nested = root / "nested"
            nested.mkdir()
            (nested / "b.Pdf").write_bytes(b"")

            self.assertEqual(
                ["A.pdf", "nested/b.Pdf", "z.PDF"],
                [path.relative_to(root).as_posix() for path in discover_source_files(
                    root, suffixes={".pdf"}, recursive=True
                )],
            )

    def test_root_level_discovery_does_not_include_nested_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "root.CHM").write_bytes(b"")
            (root / "nested").mkdir()
            (root / "nested" / "nested.chm").write_bytes(b"")

            self.assertEqual(
                [root / "root.CHM"],
                discover_source_files(root, suffixes={".chm"}, recursive=False),
            )

    def test_excluded_directory_is_not_traversed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            excluded = root / ".git"
            excluded.mkdir()
            (excluded / "hidden.pdf").write_bytes(b"")

            self.assertEqual([], discover_source_files(
                root, suffixes={".pdf"}, recursive=True
            ))

    def test_ignored_nonmatching_symlink_does_not_abort_discovery(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            target = root / "notes.txt"
            target.write_bytes(b"")
            link = root / "notes.link"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")

            self.assertEqual([], discover_source_files(
                root, suffixes={".pdf"}, recursive=True
            ))

    def test_relative_root_returns_relative_paths(self):
        with tempfile.TemporaryDirectory() as raw:
            absolute_root = Path(raw)
            (absolute_root / "file.PDF").write_bytes(b"")
            relative_root = Path(os.path.relpath(absolute_root, Path.cwd()))

            found = discover_source_files(
                relative_root, suffixes={".pdf"}, recursive=False
            )

            self.assertFalse(found[0].is_absolute())
            self.assertEqual(relative_root, found[0].parent)

    def test_rejects_matching_symlink_instead_of_following_it(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            outside = root.parent / (root.name + "-outside")
            outside.mkdir()
            target = outside / "real.pdf"
            target.write_bytes(b"")
            link = root / "linked.pdf"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                target.unlink()
                outside.rmdir()
                self.skipTest("symlinks unavailable")
            try:
                with self.assertRaises(SourceSafetyError):
                    discover_source_files(root, suffixes={".pdf"}, recursive=True)
            finally:
                target.unlink()
                outside.rmdir()

    def test_ignores_symlink_directory_without_source_suffix(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            outside = Path(raw).parent / (Path(raw).name + "-outside")
            outside.mkdir()
            (outside / "outside.pdf").write_bytes(b"")
            link = root / "linked"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                (outside / "outside.pdf").unlink()
                outside.rmdir()
                self.skipTest("symlinks unavailable")
            try:
                self.assertEqual([], discover_source_files(
                    root, suffixes={".pdf"}, recursive=True
                ))
            finally:
                (outside / "outside.pdf").unlink()
                outside.rmdir()

    def test_symlinked_directory_with_source_suffix_is_not_a_regular_candidate(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            outside = root.parent / (root.name + "-directory")
            outside.mkdir()
            link = root / "linked.pdf"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                outside.rmdir()
                self.skipTest("symlinks unavailable")
            try:
                self.assertEqual([], discover_source_files(
                    root, suffixes={".pdf"}, recursive=True
                ))
            finally:
                outside.rmdir()

    def test_relative_root_with_dotdot_is_normalized_for_consumers(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            nested = root / "nested"
            nested.mkdir()
            (root / "guide.pdf").write_bytes(b"")
            caller_root = nested / ".."
            found = discover_source_files(
                caller_root, suffixes={".pdf"}, recursive=True
            )
            self.assertEqual([root / "guide.pdf"], found)

    def test_source_path_chain_helper_detects_ancestor_links(self):
        from source_safety import first_link_in_path

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.assertIsNone(first_link_in_path(root / "child" / "file.pdf"))

    def test_tree_validator_rejects_descendant_link_without_following_it(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "topic.htm").write_text("topic", encoding="utf-8")
            descendant = root / "linked.htm"
            descendant.write_text("linked", encoding="utf-8")

            def fake_first_link(path):
                return descendant if Path(path) == descendant else None

            with mock.patch("source_safety.first_link_in_path", side_effect=fake_first_link), \
                 self.assertRaises(SourceSafetyError):
                validate_source_tree(root)


if __name__ == "__main__":
    unittest.main()
