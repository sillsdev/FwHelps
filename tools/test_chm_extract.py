import tempfile
import unittest
from pathlib import Path
from unittest import mock

import chm_extract


class ChmExtractionIsolationTests(unittest.TestCase):
    def _chm(self, directory: Path) -> Path:
        chm = directory / "sample.chm"
        chm.write_bytes(b"not a real archive")
        return chm

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
            self.assertIn("truncated/unknown filename: Using_Help.brsx", fatal)

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
                (outdir / "topic.htm").write_text("partial", encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
