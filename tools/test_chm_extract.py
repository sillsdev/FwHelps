import tempfile
import unittest
from pathlib import Path
from unittest import mock

import chm_extract
from output_fs import OutputPathError
from source_safety import SourceSafetyError


class ChmExtractionIsolationTests(unittest.TestCase):
    def _chm(self, directory: Path) -> Path:
        chm = directory / "sample.chm"
        chm.write_bytes(b"not a real archive")
        return chm

    def test_rejects_destination_equal_to_source_parent(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chm = self._chm(root)
            with self.assertRaises(OutputPathError):
                chm_extract.extract(chm, root)

    def test_rejects_destination_tree_that_contains_source_chm(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chm = self._chm(root)
            with self.assertRaises(OutputPathError):
                chm_extract.extract(chm, root / ".." / root.name)

    def test_rejects_destination_link_chain_before_staging(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chm = self._chm(root)
            destination = root / "work" / "out"
            with mock.patch.object(
                chm_extract, "first_link_in_path", side_effect=[None, destination.parent]
            ), self.assertRaises(OutputPathError):
                chm_extract._validate_extract_paths(chm, destination)

    def test_rejects_source_chm_with_linked_ancestor(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chm = self._chm(root)
            with mock.patch.object(chm_extract, "first_link_in_path", return_value=root.parent), \
                 self.assertRaises(chm_extract.ExtractError):
                chm_extract._validate_extract_paths(chm, root / "work")

    def test_rejects_actual_destination_symlink_chain_when_available(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chm = self._chm(root)
            outside = root.parent / (root.name + "-destination")
            outside.mkdir()
            link = root / "linked-work"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                outside.rmdir()
                self.skipTest("symlinks unavailable")
            try:
                with self.assertRaises(OutputPathError):
                    chm_extract._validate_extract_paths(chm, link / "out")
            finally:
                outside.rmdir()

    def test_rejects_actual_source_symlink_ancestor_when_available(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            outside = root.parent / (root.name + "-source")
            outside.mkdir()
            (outside / "sample.chm").write_bytes(b"not a real archive")
            link = root / "linked-source"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                (outside / "sample.chm").unlink()
                outside.rmdir()
                self.skipTest("symlinks unavailable")
            try:
                with self.assertRaises(chm_extract.ExtractError):
                    chm_extract._validate_extract_paths(link / "sample.chm", root / "work")
            finally:
                (outside / "sample.chm").unlink()
                outside.rmdir()

    def test_allows_descendant_destination_that_does_not_contain_source(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chm = self._chm(root)
            destination = root / "work" / "extracted"

            def backend(_chm: Path, outdir: Path) -> str:
                (outdir / "index.htm").write_text("ok", encoding="utf-8")
                (outdir / "toc.hhc").write_text(
                    '<param name="Local" value="index.htm">', encoding="cp1252"
                )
                return "tool"

            with mock.patch.object(chm_extract, "_sevenzip", backend), \
                 mock.patch.object(chm_extract, "_chmlib", lambda *_: None), \
                 mock.patch.object(chm_extract, "_hh", lambda *_: None):
                self.assertEqual("tool", chm_extract.extract(chm, destination))

    def test_brs_companion_artifact_is_known_but_unknown_filename_is_fatal(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            valid = root / "valid"
            valid.mkdir()
            (valid / "topic.htm").write_text("topic", encoding="utf-8")
            (valid / "Using_Help.brs").write_text("companion", encoding="utf-8")
            (valid / "toc.hhc").write_text(
                '<param name="Local" value="topic.htm">', encoding="cp1252"
            )
            fatal, _ = chm_extract.validate(valid)
            self.assertEqual([], fatal)

            invalid = root / "invalid"
            invalid.mkdir()
            (invalid / "topic.htm").write_text("topic", encoding="utf-8")
            (invalid / "Using_Help.brsx").write_text("unknown", encoding="utf-8")
            (invalid / "toc.hhc").write_text(
                '<param name="Local" value="topic.htm">', encoding="cp1252"
            )
            fatal, _ = chm_extract.validate(invalid)
            self.assertEqual([], fatal)

    def test_hhc_local_param_is_casefolded_and_attribute_order_independent(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "Texts_&_Words.htm").write_text("topic", encoding="cp1252")
            (root / "book.hhc").write_text(
                "<OBJECT><PARAM VALUE='Texts_%26_Words.htm' data-x='1' NAME='LOCAL'>"
                "</OBJECT>", encoding="cp1252"
            )
            fatal, advisory = chm_extract.validate(root)
        self.assertEqual([], fatal)
        self.assertEqual([], advisory)

    def test_html_href_and_src_references_detect_missing_and_truncated_targets(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "page.htm").write_text(
                "<a href='missing.htm'>missing</a>"
                "<img SRC=\"clip-image.png\">", encoding="cp1252"
            )
            (root / "clip-image.pn").write_bytes(b"truncated")
            (root / "book.hhc").write_text(
                "<param name='Local' value='page.htm'>", encoding="cp1252"
            )
            fatal, advisory = chm_extract.validate(root)
        self.assertTrue(any("clip-image.png" in item for item in fatal))
        self.assertTrue(any("missing.htm" in item for item in advisory))

    def test_unrelated_asset_extensions_are_not_truncation_failures(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "page.htm").write_text("page", encoding="cp1252")
            for name in ("font.woff", "picture.webp", "data.json", "bundle.map"):
                (root / name).write_text("asset", encoding="utf-8")
            (root / "book.hhc").write_text(
                "<param name='Local' value='page.htm'>", encoding="cp1252"
            )
            fatal, _ = chm_extract.validate(root)
        self.assertEqual([], fatal)

    def test_unsafe_and_absolute_references_are_fatal_with_specific_codes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "page.htm").write_text(
                "<a href='javascript:alert(1)'>x</a>"
                "<img src='C:\\\\outside.png'>"
                "<a href='/outside.htm'>x</a>", encoding="cp1252"
            )
            (root / "book.hhc").write_text(
                "<param name='Local' value='page.htm'>", encoding="cp1252"
            )
            fatal, advisory = chm_extract.validate(root)
        self.assertFalse(any("unsafe_uri" in item or "path_escape" in item for item in fatal))
        self.assertTrue(any("source_unsafe_uri" in item for item in advisory))
        self.assertTrue(any("source_path_escape" in item for item in advisory))

    def test_encoded_uri_separators_cannot_hide_unsafe_schemes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "page.htm").write_text(
                "<a href='java&#9;script:alert(1)'>x</a>"
                "<a href='%6a%61%76%61%73%63%72%69%70%74:alert(1)'>x</a>",
                encoding="cp1252",
            )
            (root / "book.hhc").write_text(
                "<param name='Local' value='page.htm'>", encoding="cp1252"
            )
            fatal, advisory = chm_extract.validate(root)
        self.assertEqual([], fatal)
        self.assertGreaterEqual(sum("source_unsafe_uri" in item for item in advisory), 2)

    def test_backends_get_private_empty_dirs_and_only_valid_result_is_promoted(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chm = self._chm(root)
            destination = root / "extracted"
            seen: list[tuple[str, Path, list[str]]] = []

            def failed_backend(_chm: Path, outdir: Path) -> str:
                seen.append(("failed", outdir, sorted(p.name for p in outdir.iterdir())))
                (outdir / "contamination.htm").write_text("bad", encoding="utf-8")
                return "failed-tool"

            def successful_backend(_chm: Path, outdir: Path) -> str:
                seen.append(("successful", outdir, sorted(p.name for p in outdir.iterdir())))
                (outdir / "index.htm").write_text("good", encoding="utf-8")
                (outdir / "toc.hhc").write_text(
                    '<param name="Local" value="index.htm">', encoding="cp1252"
                )
                return "successful-tool"

            with mock.patch.object(chm_extract, "_sevenzip", failed_backend), \
                 mock.patch.object(chm_extract, "_chmlib", successful_backend), \
                 mock.patch.object(chm_extract, "_hh", lambda *_: None):
                self.assertEqual("successful-tool", chm_extract.extract(chm, destination))

            self.assertEqual(["failed", "successful"], [item[0] for item in seen])
            self.assertEqual([[], []], [item[2] for item in seen])
            self.assertNotEqual(seen[0][1], seen[1][1])
            self.assertTrue(all(item[1].parent == destination.parent for item in seen))
            self.assertEqual("good", (destination / "index.htm").read_text(encoding="utf-8"))
            self.assertFalse((destination / "contamination.htm").exists())

    def test_failed_attempts_leave_existing_destination_untouched(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chm = self._chm(root)
            destination = root / "extracted"
            destination.mkdir()
            marker = destination / "previous.htm"
            marker.write_text("keep", encoding="utf-8")

            def invalid_backend(_chm: Path, outdir: Path) -> str:
                (outdir / "new.htm").write_text("invalid", encoding="utf-8")
                return "invalid-tool"

            with (
                mock.patch.object(chm_extract, "_sevenzip", invalid_backend),
                mock.patch.object(chm_extract, "_chmlib", lambda *_: None),
                mock.patch.object(chm_extract, "_hh", lambda *_: None),
                self.assertRaises(chm_extract.ExtractError),
            ):
                chm_extract.extract(chm, destination)

            self.assertEqual("keep", marker.read_text(encoding="utf-8"))
            self.assertFalse((destination / "new.htm").exists())

    def test_clean_false_keeps_existing_extraction_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chm = self._chm(root)
            destination = root / "extracted"
            destination.mkdir()
            (destination / "old.htm").write_text("old", encoding="utf-8")

            def backend(_chm: Path, outdir: Path) -> str:
                (outdir / "new.htm").write_text("new", encoding="utf-8")
                (outdir / "toc.hhc").write_text(
                    '<param name="Local" value="new.htm">', encoding="cp1252"
                )
                return "tool"

            with mock.patch.object(chm_extract, "_sevenzip", backend), \
                 mock.patch.object(chm_extract, "_chmlib", lambda *_: None), \
                 mock.patch.object(chm_extract, "_hh", lambda *_: None):
                self.assertEqual("tool", chm_extract.extract(chm, destination, clean=False))

            self.assertTrue((destination / "old.htm").exists())
            self.assertTrue((destination / "new.htm").exists())

    def test_clean_false_rejects_linked_existing_descendant_before_copy(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chm = self._chm(root)
            destination = root / "extracted"
            destination.mkdir()
            outside = root.parent / (root.name + "-existing-link")
            outside.mkdir()
            linked = destination / "linked"
            try:
                linked.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                outside.rmdir()
                self.skipTest("symlinks unavailable")

            def backend(_chm, outdir):
                (outdir / "new.htm").write_text("new", encoding="utf-8")
                (outdir / "toc.hhc").write_text(
                    '<param name="Local" value="new.htm">', encoding="cp1252"
                )
                return "tool"

            try:
                with mock.patch.object(chm_extract, "_sevenzip", backend), \
                     mock.patch.object(chm_extract, "_chmlib", lambda *_: None), \
                     mock.patch.object(chm_extract, "_hh", lambda *_: None), \
                     self.assertRaises(SourceSafetyError):
                    chm_extract.extract(chm, destination, clean=False)
                self.assertFalse((destination / "new.htm").exists())
            finally:
                linked.unlink(missing_ok=True)
                outside.rmdir()

    def test_advisory_metadata_survives_successful_promotion(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chm = self._chm(root)
            destination = root / "extracted"

            def backend(_chm: Path, outdir: Path) -> str:
                (outdir / "new.htm").write_text("new", encoding="utf-8")
                (outdir / "toc.hhc").write_text(
                    '<param name="Local" value="missing.htm">', encoding="cp1252"
                )
                return "tool"

            with mock.patch.object(chm_extract, "_sevenzip", backend), \
                 mock.patch.object(chm_extract, "_chmlib", lambda *_: None), \
                 mock.patch.object(chm_extract, "_hh", lambda *_: None):
                self.assertEqual("tool", chm_extract.extract(chm, destination))

            self.assertEqual(
                ["TOC points at a topic that does not exist: missing.htm"],
                chm_extract.extract.advisory,
            )

    def test_called_process_error_falls_back_to_next_backend(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chm = self._chm(root)
            destination = root / "extracted"

            def failed_backend(_chm: Path, _outdir: Path) -> str:
                raise chm_extract.subprocess.CalledProcessError(7, "fake")

            def successful_backend(_chm: Path, outdir: Path) -> str:
                (outdir / "index.htm").write_text("good", encoding="utf-8")
                (outdir / "toc.hhc").write_text(
                    '<param name="Local" value="index.htm">', encoding="cp1252"
                )
                return "fallback-tool"

            with mock.patch.object(chm_extract, "_sevenzip", failed_backend), \
                 mock.patch.object(chm_extract, "_chmlib", successful_backend), \
                 mock.patch.object(chm_extract, "_hh", lambda *_: None):
                self.assertEqual("fallback-tool", chm_extract.extract(chm, destination))

    def test_validation_fatal_falls_back_and_truncated_name_is_not_promoted(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chm = self._chm(root)
            destination = root / "extracted"

            def truncated_backend(_chm: Path, outdir: Path) -> str:
                (outdir / "topic").write_text("truncated", encoding="utf-8")
                (outdir / "toc.hhc").write_text(
                    '<param name="Local" value="topic.htm">', encoding="cp1252"
                )
                return "truncated-tool"

            def successful_backend(_chm: Path, outdir: Path) -> str:
                (outdir / "index.html").write_text("good", encoding="utf-8")
                (outdir / "toc.hhc").write_text(
                    '<param name="Local" value="index.html">', encoding="cp1252"
                )
                return "html-tool"

            with mock.patch.object(chm_extract, "_sevenzip", truncated_backend), \
                 mock.patch.object(chm_extract, "_chmlib", successful_backend), \
                 mock.patch.object(chm_extract, "_hh", lambda *_: None):
                self.assertEqual("html-tool", chm_extract.extract(chm, destination))

            self.assertTrue((destination / "index.html").exists())
            self.assertFalse((destination / "topic").exists())

    def test_clean_false_merge_failure_preserves_previous_destination(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chm = self._chm(root)
            destination = root / "extracted"
            destination.mkdir()
            marker = destination / "previous.htm"
            marker.write_text("keep", encoding="utf-8")

            def backend(_chm: Path, outdir: Path) -> str:
                (outdir / "new.htm").write_text("new", encoding="utf-8")
                (outdir / "toc.hhc").write_text(
                    '<param name="Local" value="new.htm">', encoding="cp1252"
                )
                return "tool"

            with mock.patch.object(chm_extract, "_sevenzip", backend), \
                 mock.patch.object(chm_extract, "_chmlib", lambda *_: None), \
                 mock.patch.object(chm_extract, "_hh", lambda *_: None), \
                 mock.patch.object(
                     chm_extract.shutil,
                     "copytree",
                     side_effect=[None, OSError("merge failed")],
                 ), self.assertRaises(OSError):
                chm_extract.extract(chm, destination, clean=False)

            self.assertEqual("keep", marker.read_text(encoding="utf-8"))
            self.assertFalse((destination / "new.htm").exists())

    def test_fresh_backend_tree_safety_failure_preserves_existing_destination(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            chm = self._chm(root)
            destination = root / "extracted"
            destination.mkdir()
            (destination / "previous.htm").write_text("keep", encoding="utf-8")

            def backend(_chm: Path, outdir: Path) -> str:
                (outdir / "new.htm").write_text("new", encoding="utf-8")
                return "unsafe-tool"

            with mock.patch.object(chm_extract, "_sevenzip", backend), \
                 mock.patch.object(chm_extract, "_chmlib", lambda *_: None), \
                 mock.patch.object(chm_extract, "_hh", lambda *_: None), \
                 mock.patch.object(
                     chm_extract, "validate_source_tree",
                     side_effect=SourceSafetyError("unsafe tree"),
                 ), self.assertRaises(SourceSafetyError):
                chm_extract.extract(chm, destination)

            self.assertEqual("keep", (destination / "previous.htm").read_text(encoding="utf-8"))
            self.assertFalse((destination / "new.htm").exists())


if __name__ == "__main__":
    unittest.main()
