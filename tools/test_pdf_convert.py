import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import pdf_convert
from frontmatter import yaml_scalar
from output_fs import ExportBusyError, ExportLock


class StandaloneConsoleTests(unittest.TestCase):
    def test_main_renders_catalog_labels_and_preserves_source_provenance(self):
        producer_report = {
            "pdf_failures": [["broken.pdf", "backend failed"]],
            "pdf_source_replacements": [["source.pdf", [{"page": 2, "count": 1}]]],
        }
        output = StringIO()
        with mock.patch.object(
            pdf_convert, "run", return_value=({"converted": 1, "report": producer_report}, [])
        ), mock.patch.object(
            sys, "argv", ["pdf_convert.py", "--repo", ".", "--out", "export"]
        ), redirect_stdout(output):
            status = pdf_convert.main()

        rendered = output.getvalue()
        self.assertEqual(1, status)
        self.assertIn("PDF conversion failure", rendered)
        self.assertIn("Replacement character", rendered)
        self.assertIn("FATAL", rendered)
        self.assertIn("WARN", rendered)
        issues = pdf_convert._canonical_report(producer_report).issues
        source_issue = next(issue for issue in issues if issue.code == "source_replacement_character")
        self.assertEqual("source", source_issue.provenance)


class StripContentsSectionsTests(unittest.TestCase):
    def test_removes_front_matter_contents_table_as_one_section(self):
        markdown = """Preface text.

## Contents

<table>
<tr><td>Introduction</td><td>1</td></tr>
</table>

## 1 Introduction

Body text.
"""

        self.assertEqual(
            "Preface text.\n\n## 1 Introduction\n\nBody text.",
            pdf_convert.strip_contents_sections(markdown),
        )

    def test_keeps_a_contents_section_in_the_body(self):
        prefix = "\n".join(f"body line {i}" for i in range(20))
        markdown = f"""{prefix}

## Contents

This section explains package contents.
"""

        self.assertIn("This section explains package contents.",
                      pdf_convert.strip_contents_sections(markdown))

    def test_discover_pdfs_rejects_symlink_inputs(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            target = repo / "real.pdf"
            target.write_bytes(b"")
            link = repo / "linked.pdf"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            with self.assertRaises(ValueError):
                pdf_convert.discover_pdfs(repo)

    def test_discover_pdfs_excludes_git_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            (repo / ".git").mkdir()
            (repo / ".git" / "hidden.pdf").write_bytes(b"")
            self.assertEqual([], pdf_convert.discover_pdfs(repo))

    def test_discover_pdfs_ignores_irrelevant_git_symlink(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            outside = repo.parent / (repo.name + "-git")
            outside.mkdir()
            (outside / "hidden.pdf").write_bytes(b"")
            link = repo / ".git"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                outside.joinpath("hidden.pdf").unlink()
                outside.rmdir()
                self.skipTest("symlinks unavailable")
            try:
                self.assertEqual([], pdf_convert.discover_pdfs(repo))
            finally:
                (outside / "hidden.pdf").unlink()
                outside.rmdir()

    def test_discover_pdfs_preserves_relative_root_style(self):
        with tempfile.TemporaryDirectory() as raw:
            absolute_root = Path(raw)
            (absolute_root / "guide.PDF").write_bytes(b"")
            relative_root = Path(os.path.relpath(absolute_root, Path.cwd()))
            found = pdf_convert.discover_pdfs(relative_root)
            self.assertFalse(found[0].is_absolute())
            self.assertEqual(relative_root, found[0].parent)

    def test_discover_pdfs_normalizes_root_with_dotdot_for_collisions(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            nested = root / "nested"
            nested.mkdir()
            (root / "guide.pdf").write_bytes(b"")
            caller_root = nested / ".."
            found = pdf_convert.discover_pdfs(caller_root)
            self.assertEqual([root / "guide.pdf"], found)
            self.assertEqual([], pdf_convert.destination_collisions(caller_root, root / "out", found))

    def test_colon_form_stops_before_editorial_front_matter(self):
        markdown = """## Contents:

1 Introduction ........ 2

Editor's note: This edition was converted from HTML.

## Abstract

Abstract body.
"""

        result = pdf_convert.strip_contents_sections(markdown)

        self.assertNotIn("1 Introduction", result)
        self.assertIn("Editor's note: This edition was converted from HTML.", result)
        self.assertIn("## Abstract", result)

    def test_keeps_section_when_no_safe_closing_boundary_exists(self):
        markdown = """# Contents

Chapter One ........ 2

## Chapter One

Real body text.
"""

        self.assertEqual(markdown.rstrip(),
                         pdf_convert.strip_contents_sections(markdown))


class StripTocTests(unittest.TestCase):
    def test_keeps_dotted_reference_line_outside_front_matter(self):
        prefix = "\n".join(f"body line {i}" for i in range(20))
        markdown = f"""{prefix}

Example ................................ 42

More body.
"""

        self.assertIn("Example ................................ 42",
                      pdf_convert.strip_toc(markdown))


class PickTitleTests(unittest.TestCase):
    def test_rejects_stale_readme_metadata_for_document_heading(self):
        self.assertEqual(
            "Technical Notes on FieldWorks Send-Receive",
            pdf_convert.pick_title(
                "FieldWorks Language Explorer beta 0.8 ReadMe",
                "# **Technical Notes on FieldWorks Send-Receive**\n\nBody.",
                "Technical Notes on FieldWorks Send-Receive",
            ),
        )

    def test_rejects_machine_generated_word_metadata(self):
        self.assertEqual(
            "Parsing With Left-Corner and Head-Driven Strategies",
            pdf_convert.pick_title(
                "Microsoft Word - ESR 041silwp1997-007r.doc",
                "# Parsing With Left-Corner and Head-Driven Strategies\n",
                "silewp2007_002",
            ),
        )

    def test_prefers_descriptive_filename_to_truncated_metadata(self):
        self.assertEqual(
            "FieldWorks Writing Systems",
            pdf_convert.pick_title(
                "Writing Systems",
                "Body without a heading.",
                "FieldWorks Writing Systems",
            ),
        )

    def test_uses_title_line_before_a_generic_contents_bookmark(self):
        self.assertEqual(
            "TonePars: A Computational Tool for Exploring Autosegmental Tonology",
            pdf_convert.pick_title(
                "Microsoft Word - ESR 041silwp1997-007r.doc",
                """**TonePars** : A Computational Tool for Exploring Autosegmental Tonology

H. Andrew Black

#### Contents:
""",
                "silewp2007_002",
            ),
        )

    def test_ignores_numbered_running_header_before_document_title(self):
        self.assertEqual(
            "Technical Notes on Writing Systems",
            pdf_convert.pick_title(
                "FieldWorks Language Explorer beta 0.8 ReadMe",
                """1 Ingredients

1

## Technical Notes on Writing Systems
""",
                "Technical Notes on Writing Systems",
            ),
        )


class DropRepeatedTitleTests(unittest.TestCase):
    def test_removes_plain_emphasized_title_line(self):
        markdown = """**TonePars** : A Computational Tool for Exploring Autosegmental Tonology

H. Andrew Black

#### Contents:
"""

        self.assertEqual(
            "H. Andrew Black\n\n#### Contents:\n",
            pdf_convert.drop_repeated_title(
                markdown,
                "TonePars: A Computational Tool for Exploring Autosegmental Tonology",
            ),
        )


class FinalizePdfTests(unittest.TestCase):
    def test_outline_describes_body_after_repeated_title_removal(self):
        title, body, outline = pdf_convert.finalize_pdf(
            "Document Title",
            "## Document Title\n\n## 1 Introduction\n\nBody.\n",
            "Document Title",
        )

        self.assertEqual("Document Title", title)
        self.assertNotIn("## Document Title", body)
        self.assertEqual([(2, "1 Introduction")], outline)

    def test_removes_every_copy_of_a_title_split_across_lines(self):
        markdown = """## A Conceptual Introduction to Morphological Parsing for

**FieldWorks Language Explorer**

## A Conceptual Introduction to Morphological Parsing for

**FieldWorks Language Explorer**

H. Andrew Black
"""

        self.assertEqual(
            "H. Andrew Black\n",
            pdf_convert.drop_repeated_title(
                markdown,
                "A Conceptual Introduction to Morphological Parsing for FieldWorks Language Explorer",
            ),
        )

    def test_normalizes_author_and_top_level_heading(self):
        title, body, outline = pdf_convert.finalize_pdf(
            "Publishing",
            "##### Ken Zook\n\n### 1 Introduction\n\nBody.\n",
            "Publishing",
        )
        self.assertEqual("Publishing", title)
        self.assertNotIn("Ken Zook", body)
        self.assertEqual([(2, "1 Introduction")], outline)

    def test_promotes_documents_that_begin_at_h3_without_losing_nested_levels(self):
        _, body, outline = pdf_convert.finalize_pdf(
            "Variant Generator",
            "### 1 Introduction\n\n#### 1.1 Appearance\n\nBody.\n",
            "VarGen",
        )
        self.assertIn("## 1 Introduction", body)
        self.assertEqual([(2, "1 Introduction"), (3, "1.1 Appearance")], outline)

    def test_drops_standalone_equation_labels_but_keeps_prose_headings(self):
        _, body, outline = pdf_convert.finalize_pdf(
            "Tone",
            "## 2 Concepts\n\n#### (4)\n\nEquation prose.\n\n## 3 Results\n",
            "silewp2007_002",
        )
        self.assertNotIn("#### (4)", body)
        self.assertEqual([(2, "2 Concepts"), (2, "3 Results")], outline)


class OutlineLockTests(unittest.TestCase):
    def test_run_waits_on_global_outline_lock_even_for_different_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            out = Path(tmp) / "out"
            repo.mkdir()
            with ExportLock(pdf_convert.OUTLINES), self.assertRaises(ExportBusyError):
                pdf_convert.run(repo, out, update=False)
            self.assertFalse(out.exists())

    def test_update_and_reader_share_global_outline_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            first = Path(tmp) / "first"
            second = Path(tmp) / "second"
            with ExportLock(pdf_convert.OUTLINES):
                with self.assertRaises(ExportBusyError):
                    pdf_convert.run(repo, first, update=True)
                with self.assertRaises(ExportBusyError):
                    pdf_convert.run(repo, second, update=False)
    def test_slug_path_replaces_windows_unsafe_and_reserved_names(self):
        self.assertEqual("bad_name_.pdf", pdf_convert.slug_path("bad:name?.pdf"))
        self.assertEqual("CON_.pdf", pdf_convert.slug_path("CON .pdf"))
        self.assertEqual("_CON.txt", pdf_convert.slug_path("CON.txt"))
        self.assertEqual("name.pdf_", pdf_convert.slug_path("name.pdf "))

    def test_slug_sanitization_collisions_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            out = Path(tmp) / "out"
            repo.mkdir()
            first = repo / "CON.pdf"
            second = repo / "_CON.pdf"
            first.write_bytes(b"pdf")
            second.write_bytes(b"pdf")
            collisions = pdf_convert.destination_collisions(repo, out, [first, second])
        self.assertEqual(1, len(collisions))

    def test_run_rejects_linked_outlines_before_reading_or_output_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            out = Path(tmp) / "out"
            repo.mkdir()
            linked = repo / "locks.json"
            linked.write_text("{}", encoding="utf-8")

            def mocked_link(path):
                return path if path == linked else None

            with mock.patch.object(pdf_convert, "OUTLINES", linked), \
                 mock.patch.object(pdf_convert, "first_link_in_path", side_effect=mocked_link), \
                 mock.patch.object(Path, "read_text", side_effect=AssertionError("lock was read")), \
                 self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.run(repo, out, update=False)
            self.assertFalse(out.exists())

    def test_promote_rejects_linked_lock_before_output_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            target = Path(tmp) / "keep-lock.json"
            target.write_text('{"keep":true}', encoding="utf-8")
            lock = Path(tmp) / "locks.json"

            def mocked_link(path):
                return path if path == lock else None

            with mock.patch.object(pdf_convert, "first_link_in_path", side_effect=mocked_link), \
                 self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.promote_pdf_outputs(
                    out, Path(tmp) / "stage", {}, {}, lock_path=lock,
                )
            self.assertEqual('{"keep":true}', target.read_text(encoding="utf-8"))

    def test_promote_rejects_real_linked_lock_before_output_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            target = Path(tmp) / "keep-lock.json"
            target.write_text('{"keep":true}', encoding="utf-8")
            lock = Path(tmp) / "locks.json"
            try:
                lock.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            with self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.promote_pdf_outputs(
                    out, Path(tmp) / "stage", {}, {}, lock_path=lock,
                )
            self.assertEqual('{"keep":true}', target.read_text(encoding="utf-8"))

    def test_promote_rejects_parent_traversing_lock_before_output_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            lock = out / ".." / "locks.json"
            with self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.promote_pdf_outputs(
                    out, Path(tmp) / "stage", {}, {}, lock_path=lock,
                )
            self.assertFalse((Path(tmp) / "locks.json").exists())

    def test_normalized_outline_preserves_order_and_text(self):
        self.assertEqual(
            [[2, "Introduction"], [3, "A heading"], [2, "Introduction"]],
            pdf_convert.normalize_outline(
                [(2, " Introduction "), (3, "A   heading"), (2, "Introduction")]
            ),
        )

    def test_outline_lock_compares_complete_ordered_outline(self):
        pin = {"outline": [[2, "One"], [2, "Two"]]}
        self.assertTrue(pdf_convert.outline_matches(pin, [(2, "One"), (2, "Two")]))
        self.assertFalse(pdf_convert.outline_matches(pin, [(2, "One"), (2, "Other")]))
        self.assertFalse(pdf_convert.outline_matches(pin, [(2, "Two"), (2, "One")]))
        self.assertFalse(pdf_convert.outline_matches(pin, [(3, "One"), (2, "Two")]))
        self.assertFalse(pdf_convert.outline_matches(pin, [(2, "One"), (2, "Two"), (2, "Three")]))
        self.assertFalse(pdf_convert.outline_matches(pin, [(2, "One")]))


class PdfTraceabilityTests(unittest.TestCase):
    def test_yaml_scalar_escapes_controls_and_preserves_unicode(self):
        for value in ('line\nnext\r\x00"é', "quote\\slash"):
            encoded = yaml_scalar(value)
            self.assertEqual(value, json.loads(encoded))
            self.assertNotRegex(encoded, r"[\x00-\x1f]")

    def test_frontmatter_uses_safe_scalars_at_nested_levels(self):
        result = pdf_convert.frontmatter({
            "title": 'line\nnext\x00"é',
            "metadata": {"author": "A\rB"},
            "tags": ["one\n two", "é"],
        })
        self.assertIn('title: "line\\nnext\\u0000\\\"é"', result)
        self.assertIn('  author: "A\\rB"', result)
        self.assertIn('  - "one\\n two"', result)
        self.assertNotRegex(result, r"[\x00\r\x01-\x08\x0b\x0c\x0e-\x1f]")

    def test_source_url_encodes_relative_path_segments(self):
        self.assertEqual(
            "https://example.test/blob/main/docs/My%20file%20%5Bx%5D.pdf",
            pdf_convert._source_url(
                "docs/My file [x].pdf",
                "https://example.test/blob/main/{path}",
            ),
        )

    def test_frontmatter_serializes_pdf_traceability_fields(self):
        result = pdf_convert.frontmatter(
            {
                "source": "docs/example.pdf",
                "source_url": "https://example.test/blob/main/docs/example.pdf",
                "sha256": "abc123",
                "pdf_metadata": {"title": "Example", "author": "A"},
                "structure": "bookmarks (2p)",
                "outline_count": 3,
            }
        )
        self.assertIn('source_url: "https://example.test/blob/main/docs/example.pdf"', result)
        self.assertIn('sha256: "abc123"', result)
        self.assertIn('pdf_metadata:', result)
        self.assertIn('  title: "Example"', result)


class ReplacementCharacterTests(unittest.TestCase):
    def test_source_control_glyph_is_provenanced_as_replacement_risk(self):
        self.assertEqual(
            {"count": 1, "codepoints": ["U+001F"]},
            pdf_convert.source_replacement_details("valid\x1f text"),
        )

    def test_source_replacements_are_provenanced_but_exporter_replacements_are_fatal(self):
        source = [{"page": 21, "count": 1}]
        self.assertEqual(
            {"source_count": 1, "exporter_count": 0},
            pdf_convert.replacement_provenance(source, 1),
        )
        self.assertEqual(
            {"source_count": 0, "exporter_count": 1},
            pdf_convert.replacement_provenance([], 1),
        )

    def test_run_rejects_exporter_created_replacement_character(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            out = Path(tmp) / "out"
            repo.mkdir()
            (repo / "one.pdf").write_bytes(b"one")

            class FakeDoc:
                def __init__(self):
                    self.metadata = {"title": "One"}

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            with mock.patch.object(
                pdf_convert, "convert_pdf",
                return_value=("## New\n�\n", "font-inference (1p)", []),
            ), mock.patch.object(pdf_convert.fitz, "open", return_value=FakeDoc()), \
                 mock.patch.object(pdf_convert, "OUTLINES", repo / "locks.json"):
                (repo / "locks.json").write_text("{}", encoding="utf-8")
                result, _ = pdf_convert.run(repo, out, update=True)
            self.assertEqual(1, result["report"]["pdf_export_replacements"][0][1]["exporter_count"])
            self.assertFalse((out / "one.md").exists())


class PdfOutputSafetyTests(unittest.TestCase):
    def test_schema_one_manifest_is_rejected_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            victim = out / "victim.md"
            victim.write_text("keep", encoding="utf-8")
            manifest = out / pdf_convert.PDF_MANIFEST
            manifest.write_text(
                '{"files":{"victim.pdf":{"markdown":"victim.md",'
                '"images":"victim_images"}}}', encoding="utf-8"
            )
            before = sorted(
                (p.relative_to(out).as_posix(), p.read_bytes())
                for p in out.rglob("*") if p.is_file()
            )
            with self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.promote_pdf_outputs(
                    out, Path(tmp) / "stage", {}, {},
                )
            after = sorted(
                (p.relative_to(out).as_posix(), p.read_bytes())
                for p in out.rglob("*") if p.is_file()
            )
            self.assertEqual(before, after)

    def test_corrupt_manifest_cannot_delete_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            victim = out / "unrelated"
            victim.mkdir()
            (victim / "keep.txt").write_text("keep", encoding="utf-8")
            manifest = out / pdf_convert.PDF_MANIFEST
            manifest.write_text(
                '{"schema":2,"files":{"guide.pdf":{"markdown":"guide.md",'
                '"images":"unrelated"}}}', encoding="utf-8"
            )
            with self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.promote_pdf_outputs(
                    out, Path(tmp) / "stage", {}, {},
                )
            self.assertTrue((victim / "keep.txt").exists())

    def test_corrupt_manifest_cannot_replace_unrelated_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            victim = out / "unrelated.md"
            victim.write_text("keep", encoding="utf-8")
            manifest = out / pdf_convert.PDF_MANIFEST
            manifest.write_text(
                '{"schema":2,"files":{"guide.pdf":{"markdown":"unrelated.md",'
                '"images":"guide_images"}}}', encoding="utf-8"
            )
            with self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.promote_pdf_outputs(
                    out, Path(tmp) / "stage", {}, {},
                )
            self.assertEqual("keep", victim.read_text(encoding="utf-8"))

    def test_manifest_rejects_source_destination_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            (out / pdf_convert.PDF_MANIFEST).write_text(
                '{"schema":2,"files":{"guide.pdf":{"markdown":"other.md",'
                '"images":"guide_images"}}}', encoding="utf-8"
            )
            with self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.promote_pdf_outputs(
                    out, Path(tmp) / "stage", {}, {},
                )

    def test_null_images_rejects_unclaimed_existing_canonical_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            victim = out / "doc_images"
            victim.mkdir()
            (victim / "keep.txt").write_text("keep", encoding="utf-8")
            with self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.promote_pdf_outputs(
                    out, Path(tmp) / "stage", {},
                    {"doc.pdf": {"markdown": "doc.md", "images": None}},
                )
            self.assertEqual("keep", (victim / "keep.txt").read_text(encoding="utf-8"))

    def test_canonical_markdown_symlink_cannot_redirect_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            keep = out / "keep.md"
            keep.write_text("keep", encoding="utf-8")
            link = out / "doc.md"
            try:
                link.symlink_to(keep)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            with self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.promote_pdf_outputs(
                    out, Path(tmp) / "stage", {},
                    {"doc.pdf": {"markdown": "doc.md", "images": None}},
                )
            self.assertEqual("keep", keep.read_text(encoding="utf-8"))
            self.assertTrue(link.is_symlink())

    def test_canonical_image_symlink_cannot_redirect_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            keep = out / "keep_images"
            keep.mkdir()
            (keep / "keep.txt").write_text("keep", encoding="utf-8")
            link = out / "doc_images"
            try:
                link.symlink_to(keep, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            with self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.promote_pdf_outputs(
                    out, Path(tmp) / "stage", {},
                    {"doc.pdf": {"markdown": "doc.md", "images": "doc_images"}},
                )
            self.assertEqual("keep", (keep / "keep.txt").read_text(encoding="utf-8"))

    def test_manifest_rejects_mocked_destination_link_chain_without_symlink_privilege(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            with mock.patch.object(pdf_convert, "first_link_in_path", return_value=out / "doc.md"), \
                 self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.promote_pdf_outputs(
                    out, Path(tmp) / "stage", {},
                    {"doc.pdf": {"markdown": "doc.md", "images": None}},
                )

    def test_null_images_rejects_mocked_dangling_image_link_before_exists_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()

            def mocked_link(path):
                return path if path.name == "doc_images" else None

            with mock.patch.object(pdf_convert, "first_link_in_path", side_effect=mocked_link), \
                 mock.patch.object(Path, "read_text", side_effect=AssertionError("manifest was read")), \
                 self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.promote_pdf_outputs(
                    out, Path(tmp) / "stage", {},
                    {"doc.pdf": {"markdown": "doc.md", "images": None}},
                )

    def test_dangling_canonical_image_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            link = out / "doc_images"
            try:
                link.symlink_to(out / "does-not-exist", target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            with self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.promote_pdf_outputs(
                    out, Path(tmp) / "stage", {},
                    {"doc.pdf": {"markdown": "doc.md", "images": None}},
                )

    def test_symlinked_manifest_file_is_rejected_before_reading_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            target = out / "real-manifest.json"
            target.write_text(
                '{"schema":2,"files":{"doc.pdf":{"markdown":"doc.md",'
                '"images":null}}}', encoding="utf-8"
            )
            manifest = out / pdf_convert.PDF_MANIFEST
            try:
                manifest.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            with self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.promote_pdf_outputs(
                    out, Path(tmp) / "stage", {}, {},
                )

    def test_mocked_symlinked_manifest_file_is_rejected_before_reading_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            manifest = out / pdf_convert.PDF_MANIFEST
            manifest.write_text("not json", encoding="utf-8")

            def mocked_link(path):
                return path if path == manifest else None

            with mock.patch.object(pdf_convert, "first_link_in_path", side_effect=mocked_link), \
                 mock.patch.object(Path, "read_text", side_effect=AssertionError("manifest was read")), \
                 self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.promote_pdf_outputs(out, Path(tmp) / "stage", {}, {})

    def test_case_only_source_rename_keeps_previous_output_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            (out / "Guide.md").write_text("old", encoding="utf-8")
            stage = Path(tmp) / "stage"
            stage.mkdir()
            (stage / "guide.md").write_text("new", encoding="utf-8")
            pdf_convert.promote_pdf_outputs(
                out, stage,
                {"Guide.pdf": {"markdown": "Guide.md", "images": None}},
                {"guide.pdf": {"markdown": "guide.md", "images": None}},
            )
            self.assertEqual("new", (out / "guide.md").read_text(encoding="utf-8"))
            manifest = json.loads((out / pdf_convert.PDF_MANIFEST).read_text(encoding="utf-8"))
            self.assertEqual("guide.md", manifest["files"]["guide.pdf"]["markdown"])

    def test_case_only_image_rename_allows_authenticated_image_shrink(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            (out / "Guide.md").write_text("old", encoding="utf-8")
            old_images = out / "Guide_images"
            old_images.mkdir()
            (old_images / "old.png").write_bytes(b"old")
            # On a case-sensitive filesystem, keep a second spelling to make
            # the null-image existence check exercise filesystem ownership.
            # On a case-insensitive filesystem both names address the same output.
            lower_images = out / "guide_images"
            try:
                lower_images.mkdir()
            except FileExistsError:
                pass
            (lower_images / "keep.txt").write_text("keep", encoding="utf-8")
            stage = Path(tmp) / "stage"
            stage.mkdir()
            (stage / "guide.md").write_text("new", encoding="utf-8")

            if os.path.normcase("Guide_images") != os.path.normcase("guide_images"):
                with self.assertRaises(pdf_convert.ManifestError):
                    pdf_convert.promote_pdf_outputs(
                        out, stage,
                        {"Guide.pdf": {"markdown": "Guide.md", "images": "Guide_images"}},
                        {"guide.pdf": {"markdown": "guide.md", "images": None}},
                    )
                self.assertTrue(old_images.exists())
                self.assertEqual("keep", (lower_images / "keep.txt").read_text(encoding="utf-8"))
                return

            pdf_convert.promote_pdf_outputs(
                out, stage,
                {"Guide.pdf": {"markdown": "Guide.md", "images": "Guide_images"}},
                {"guide.pdf": {"markdown": "guide.md", "images": None}},
            )
            self.assertFalse(old_images.exists())
            self.assertEqual("new", (out / "guide.md").read_text(encoding="utf-8"))

    def test_missing_staged_markdown_rejects_before_prior_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            old = out / "old.md"
            old.write_text("old", encoding="utf-8")
            with self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.promote_pdf_outputs(
                    out, Path(tmp) / "stage",
                    {"old.pdf": {"markdown": "old.md", "images": None}},
                    {"new.pdf": {"markdown": "new.md", "images": None}},
                )
            self.assertEqual("old", old.read_text(encoding="utf-8"))

    def test_nonregular_staged_markdown_rejects_before_prior_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            old = out / "old.md"
            old.write_text("old", encoding="utf-8")
            stage = Path(tmp) / "stage"
            (stage / "new.md").mkdir(parents=True)
            with self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.promote_pdf_outputs(
                    out, stage,
                    {"old.pdf": {"markdown": "old.md", "images": None}},
                    {"new.pdf": {"markdown": "new.md", "images": None}},
                )
            self.assertEqual("old", old.read_text(encoding="utf-8"))

    def test_current_images_must_be_existing_directory_before_prior_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            old = out / "old.md"
            old.write_text("old", encoding="utf-8")
            stage = Path(tmp) / "stage"
            stage.mkdir()
            (stage / "new.md").write_text("new", encoding="utf-8")
            with self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.promote_pdf_outputs(
                    out, stage,
                    {"old.pdf": {"markdown": "old.md", "images": None}},
                    {"new.pdf": {"markdown": "new.md", "images": "new_images"}},
                )
            self.assertEqual("old", old.read_text(encoding="utf-8"))

    def test_current_images_must_be_directory_before_prior_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            old = out / "old.md"
            old.write_text("old", encoding="utf-8")
            stage = Path(tmp) / "stage"
            stage.mkdir()
            (stage / "new.md").write_text("new", encoding="utf-8")
            (stage / "new_images").write_text("not a directory", encoding="utf-8")
            with self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.promote_pdf_outputs(
                    out, stage,
                    {"old.pdf": {"markdown": "old.md", "images": None}},
                    {"new.pdf": {"markdown": "new.md", "images": "new_images"}},
                )
            self.assertEqual("old", old.read_text(encoding="utf-8"))

    def test_missing_prior_markdown_rejects_before_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            stage = Path(tmp) / "stage"
            (stage / "new.md").parent.mkdir(parents=True)
            (stage / "new.md").write_text("new", encoding="utf-8")
            with self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.promote_pdf_outputs(
                    out, stage,
                    {"old.pdf": {"markdown": "old.md", "images": None}},
                    {"new.pdf": {"markdown": "new.md", "images": None}},
                )
            self.assertFalse((out / pdf_convert.PDF_MANIFEST).exists())

    def test_prior_image_tree_link_is_rejected_before_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            (out / "old.md").write_text("old", encoding="utf-8")
            images = out / "old_images"
            images.mkdir()
            (images / "real.png").write_bytes(b"real")
            nested = images / "nested"
            nested.mkdir()
            (nested / "real.png").write_bytes(b"real")
            stage = Path(tmp) / "stage"
            stage.mkdir()
            (stage / "new.md").write_text("new", encoding="utf-8")
            with mock.patch.object(
                pdf_convert, "first_link_in_path",
                side_effect=lambda path: path if path.name == "nested" else None,
            ), self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.promote_pdf_outputs(
                    out, stage,
                    {"old.pdf": {"markdown": "old.md", "images": "old_images"}},
                    {"new.pdf": {"markdown": "new.md", "images": None}},
                )
            self.assertEqual("old", (out / "old.md").read_text(encoding="utf-8"))
            self.assertEqual(b"real", (images / "real.png").read_bytes())

    def test_prior_image_nested_symlink_is_rejected_when_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            (out / "old.md").write_text("old", encoding="utf-8")
            images = out / "old_images"
            images.mkdir()
            target = out / "keep.txt"
            target.write_text("keep", encoding="utf-8")
            link = images / "nested-link"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            stage = Path(tmp) / "stage"
            stage.mkdir()
            (stage / "new.md").write_text("new", encoding="utf-8")
            with self.assertRaises(pdf_convert.ManifestError):
                pdf_convert.promote_pdf_outputs(
                    out, stage,
                    {"old.pdf": {"markdown": "old.md", "images": "old_images"}},
                    {"new.pdf": {"markdown": "new.md", "images": None}},
                )
            self.assertEqual("keep", target.read_text(encoding="utf-8"))

    def test_manifest_rejects_traversal_and_casefold_collisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            for value in (
                ('{"schema":2,"files":{"guide.pdf":{"markdown":"../x.md",'
                 '"images":"guide_images"}}}'),
                ('{"schema":2,"files":{"A.pdf":{"markdown":"a.md",'
                 '"images":"a_images"},"a.pdf":{"markdown":"a.md",'
                 '"images":"a_images"}}}'),
            ):
                (out / pdf_convert.PDF_MANIFEST).write_text(value, encoding="utf-8")
                with self.assertRaises(pdf_convert.ManifestError):
                    pdf_convert.promote_pdf_outputs(
                        out, Path(tmp) / "stage", {}, {},
                    )

    def test_destination_collisions_are_reported_before_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            out = Path(tmp) / "out"
            (repo / "a").mkdir(parents=True)
            (repo / "a b.pdf").write_bytes(b"pdf")
            (repo / "a_b.pdf").write_bytes(b"pdf")
            collisions = pdf_convert.destination_collisions(repo, out)
        self.assertEqual(1, len(collisions))
        self.assertEqual({"a b.pdf", "a_b.pdf"}, set(collisions[0][1]))

    def test_destination_collisions_casefold_output_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            out = Path(tmp) / "out"
            repo.mkdir()
            collisions = pdf_convert.destination_collisions(
                repo, out, [repo / "A.pdf", repo / "a.pdf"]
            )
        self.assertEqual(1, len(collisions))

    def test_promotion_replaces_same_source_image_directory_when_images_shrink(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            old_images = out / "doc_images"
            old_images.mkdir(parents=True)
            (out / "doc.md").write_text("old", encoding="utf-8")
            (old_images / "stale.png").write_bytes(b"old")
            stage = Path(tmp) / "stage"
            (stage / "doc_images").mkdir(parents=True)
            (stage / "doc.md").write_text("new", encoding="utf-8")
            pdf_convert.promote_pdf_outputs(
                out,
                stage,
                {"doc.pdf": {"markdown": "doc.md", "images": "doc_images"}},
                {"doc.pdf": {"markdown": "doc.md", "images": None}},
            )
            self.assertFalse((old_images / "stale.png").exists())
            self.assertFalse(old_images.exists())
            manifest = json.loads(
                (out / pdf_convert.PDF_MANIFEST).read_text(encoding="utf-8")
            )
            self.assertIsNone(manifest["files"]["doc.pdf"]["images"])

    def test_promotion_removes_old_destination_for_same_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            old = out / "old.md"
            old.parent.mkdir(parents=True)
            old.write_text("old", encoding="utf-8")
            old_images = out / "old_images"
            old_images.mkdir(parents=True)
            stage = Path(tmp) / "stage"
            (stage / "new.md").parent.mkdir(parents=True)
            (stage / "new.md").write_text("new", encoding="utf-8")
            (stage / "new_images").mkdir(parents=True)
            pdf_convert.promote_pdf_outputs(
                out,
                stage,
                {"old.pdf": {"markdown": "old.md", "images": "old_images"}},
                {"new.pdf": {"markdown": "new.md", "images": "new_images"}},
            )
            self.assertFalse(old.exists())
            self.assertEqual("new", (out / "new.md").read_text(encoding="utf-8"))

    def test_late_pdf_failure_preserves_previous_pdf_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            out = Path(tmp) / "out"
            repo.mkdir()
            (repo / "one.pdf").write_bytes(b"one")
            (repo / "two.pdf").write_bytes(b"two")
            out.mkdir()
            old = out / "one.md"
            old.write_text("previous", encoding="utf-8")
            (out / pdf_convert.PDF_MANIFEST).write_text(
                '{"schema":2,"files":{"one.pdf":{"markdown":"one.md",'
                '"images":"one_images"}}}',
                encoding="utf-8",
            )

            def fake_convert(pdf, out_md, image_dir):
                if pdf.name == "two.pdf":
                    raise RuntimeError("late failure")
                return "## New\n", "font-inference (1p)", []

            class FakeDoc:
                def __init__(self):
                    self.metadata = {"title": "One"}

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            with mock.patch.object(pdf_convert, "convert_pdf", fake_convert), \
                 mock.patch.object(pdf_convert.fitz, "open", return_value=FakeDoc()), \
                 mock.patch.object(pdf_convert, "OUTLINES", repo / "locks.json"):
                (repo / "locks.json").write_text(
                    '{"one.pdf":{"outline":[[2,"New"]]},'
                    '"two.pdf":{"outline":[[2,"New"]]}}',
                    encoding="utf-8",
                )
                result, _ = pdf_convert.run(repo, out, update=False)
            self.assertIn("two.pdf", [item[0] for item in result["report"]["pdf_failures"]])
            self.assertEqual("previous", old.read_text(encoding="utf-8"))

    def test_missing_lock_is_fatal_before_promoting_staged_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            out = Path(tmp) / "out"
            repo.mkdir()
            (repo / "ONE.PDF").write_bytes(b"one")

            class FakeDoc:
                def __init__(self):
                    self.metadata = {"title": "One"}

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            with mock.patch.object(pdf_convert, "convert_pdf",
                                   return_value=("## New\n", "font-inference (1p)", [])), \
                 mock.patch.object(pdf_convert.fitz, "open", return_value=FakeDoc()), \
                 mock.patch.object(pdf_convert, "OUTLINES", repo / "locks.json"):
                (repo / "locks.json").write_text("{}", encoding="utf-8")
                result, _ = pdf_convert.run(repo, out, update=False)
            self.assertIn("ONE.PDF", result["report"]["outline_unpinned"])
            self.assertFalse((out / "ONE.md").exists())

    def test_changed_pdf_set_prunes_stale_outputs_and_keeps_chm_sentinel(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            out = Path(tmp) / "out"
            repo.mkdir()
            one = repo / "one.pdf"
            two = repo / "two.pdf"
            one.write_bytes(b"one")
            two.write_bytes(b"two")
            sentinel = out / "CHM" / "topic.md"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text("keep", encoding="utf-8")

            class FakeDoc:
                def __init__(self):
                    self.metadata = {"title": "Document"}

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            def fake_convert(pdf, out_md, image_dir):
                image_dir.mkdir(parents=True, exist_ok=True)
                (image_dir / "figure.png").write_bytes(pdf.read_bytes())
                return "## New\n", "font-inference (1p)", []

            with mock.patch.object(pdf_convert, "convert_pdf", fake_convert), \
                 mock.patch.object(pdf_convert.fitz, "open", return_value=FakeDoc()), \
                 mock.patch.object(pdf_convert, "OUTLINES", repo / "locks.json"):
                (repo / "locks.json").write_text("{}", encoding="utf-8")
                pdf_convert.run(repo, out, update=True)
                written_manifest = json.loads(
                    (out / pdf_convert.PDF_MANIFEST).read_text(encoding="utf-8")
                )
                self.assertEqual(2, written_manifest["schema"])
                two.unlink()
                pdf_convert.run(repo, out, update=True)
            self.assertTrue((out / "one.md").exists())
            self.assertFalse((out / "two.md").exists())
            self.assertFalse((out / "two_images").exists())
            self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))

    def test_promotion_failure_restores_previous_pdf_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            old = out / "doc.md"
            old_images = out / "doc_images"
            old_images.mkdir()
            old.write_text("old", encoding="utf-8")
            (old_images / "old.png").write_bytes(b"old")
            manifest = out / pdf_convert.PDF_MANIFEST
            manifest.write_text('{"schema":2,"files":{"doc.pdf":{"markdown":"doc.md",'
                                '"images":"doc_images"}}}',
                                encoding="utf-8")
            old_manifest = manifest.read_text(encoding="utf-8")
            stage = Path(tmp) / "stage"
            (stage / "doc_images").mkdir(parents=True)
            (stage / "doc.md").write_text("new", encoding="utf-8")
            previous = {"doc.pdf": {"markdown": "doc.md", "images": "doc_images"}}
            current = {"doc.pdf": {"markdown": "doc.md", "images": "doc_images"}}
            def fail_move(*args, **kwargs):
                raise OSError("disk full")

            with mock.patch.object(pdf_convert.shutil, "move", fail_move), self.assertRaises(OSError):
                pdf_convert.promote_pdf_outputs(out, stage, previous, current)
            self.assertEqual("old", old.read_text(encoding="utf-8"))
            self.assertEqual(b"old", (old_images / "old.png").read_bytes())
            self.assertEqual(old_manifest, manifest.read_text(encoding="utf-8"))

    def test_manifest_promotion_failure_restores_previous_pdf_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            old = out / "doc.md"
            old_images = out / "doc_images"
            old_images.mkdir()
            old.write_text("old", encoding="utf-8")
            (old_images / "old.png").write_bytes(b"old")
            manifest = out / pdf_convert.PDF_MANIFEST
            manifest.write_text('{"schema":2,"files":{"doc.pdf":{"markdown":"doc.md",'
                                '"images":"doc_images"}}}',
                                encoding="utf-8")
            old_manifest = manifest.read_text(encoding="utf-8")
            stage = Path(tmp) / "stage"
            (stage / "doc_images").mkdir(parents=True)
            (stage / "doc.md").write_text("new", encoding="utf-8")
            previous = {"doc.pdf": {"markdown": "doc.md", "images": "doc_images"}}
            current = {"doc.pdf": {"markdown": "doc.md", "images": "doc_images"}}
            original_move = pdf_convert.shutil.move

            def fail_manifest(source, target):
                if Path(target) == manifest:
                    raise OSError("manifest disk full")
                return original_move(source, target)

            with mock.patch.object(pdf_convert.shutil, "move", fail_manifest), self.assertRaises(OSError):
                pdf_convert.promote_pdf_outputs(out, stage, previous, current)
            self.assertEqual("old", old.read_text(encoding="utf-8"))
            self.assertEqual(b"old", (old_images / "old.png").read_bytes())
            self.assertEqual(old_manifest, manifest.read_text(encoding="utf-8"))

    def test_lock_promotion_failure_restores_outputs_manifest_and_old_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            old = out / "doc.md"
            old_images = out / "doc_images"
            old_images.mkdir()
            old.write_text("old", encoding="utf-8")
            (old_images / "old.png").write_bytes(b"old")
            manifest = out / pdf_convert.PDF_MANIFEST
            manifest.write_text('{"schema":2,"files":{"doc.pdf":{"markdown":"doc.md",'
                                '"images":"doc_images"}}}',
                                encoding="utf-8")
            old_manifest = manifest.read_text(encoding="utf-8")
            lock = Path(tmp) / "locks.json"
            lock.write_text('{"old":true}\n', encoding="utf-8")
            old_lock = lock.read_text(encoding="utf-8")
            stage = Path(tmp) / "stage"
            (stage / "doc_images").mkdir(parents=True)
            (stage / "doc.md").write_text("new", encoding="utf-8")
            previous = {"doc.pdf": {"markdown": "doc.md", "images": "doc_images"}}
            current = {"doc.pdf": {"markdown": "doc.md", "images": "doc_images"}}
            original_move = pdf_convert.shutil.move

            def fail_lock(source, target):
                if Path(target) == lock:
                    raise OSError("lock disk full")
                return original_move(source, target)

            with mock.patch.object(pdf_convert.shutil, "move", fail_lock), self.assertRaises(OSError):
                pdf_convert.promote_pdf_outputs(
                    out, stage, previous, current, lock_path=lock,
                    fresh={"doc.pdf": {"outline": [[2, "New"]]}},
                )
            self.assertEqual("old", old.read_text(encoding="utf-8"))
            self.assertEqual(b"old", (old_images / "old.png").read_bytes())
            self.assertEqual(old_manifest, manifest.read_text(encoding="utf-8"))
            self.assertEqual(old_lock, lock.read_text(encoding="utf-8"))


class StripFurnitureTests(unittest.TestCase):
    def test_removes_a_running_header_used_on_only_two_pages(self):
        pages = [
            "# **1 Ingredients**\n\n1\n\nFirst page body.",
            "# **1 Ingredients**\n\n2\n\nSecond page body.",
        ] + [f"# **Section {i}**\n\n{i}\n\nPage {i} body." for i in range(3, 14)]

        cleaned = pdf_convert.strip_furniture(pages)

        self.assertEqual("First page body.", cleaned[0])
        self.assertEqual("Second page body.", cleaned[1])

    def test_keeps_distinct_numbered_headings_at_page_boundaries(self):
        pages = [
            "Scenario 1\n\nFirst scenario body.",
            "Scenario 2\n\nSecond scenario body.",
            "Different heading\n\nThird body.",
        ]

        self.assertTrue(pdf_convert.strip_furniture(pages)[0].startswith("Scenario 1"))
        self.assertTrue(pdf_convert.strip_furniture(pages)[1].startswith("Scenario 2"))

    def test_keeps_repeated_boundary_prose(self):
        pages = [
            "First page body.\n\nClick OK.",
            "Second page body.\n\nClick OK.",
            "Third page body.\n\nDifferent ending.",
        ]

        cleaned = pdf_convert.strip_furniture(pages)

        self.assertTrue(cleaned[0].endswith("Click OK."))
        self.assertTrue(cleaned[1].endswith("Click OK."))


class LuaTableTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("pandoc"), "pandoc is required")
    def test_headerless_table_keeps_a_neutral_header(self):
        html = ("<table><tr><td>Alpha</td><td>1</td></tr>"
                "<tr><td>Beta</td><td>2</td></tr></table>")
        result = subprocess.run(
            ["pandoc", "-f", "html", "-t", "gfm", "--wrap=none",
             f"--lua-filter={Path(__file__).with_name('fwhelp.lua')}"],
            input=html,
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8",
        )

        self.assertRegex(result.stdout.splitlines()[0], r"^\|\s*\|\s*\|$")


if __name__ == "__main__":
    unittest.main()
