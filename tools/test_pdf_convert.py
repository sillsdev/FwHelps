import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pdf_convert


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
            (old_images / "stale.png").write_bytes(b"old")
            stage = Path(tmp) / "stage"
            (stage / "doc_images").mkdir(parents=True)
            (stage / "doc.md").write_text("new", encoding="utf-8")
            pdf_convert.promote_pdf_outputs(
                out,
                stage,
                {"doc.pdf": {"markdown": "doc.md", "images": "doc_images"}},
                {"doc.pdf": {"markdown": "doc.md", "images": "doc_images"}},
            )
            self.assertFalse((old_images / "stale.png").exists())
            self.assertFalse(old_images.exists())

    def test_promotion_removes_old_destination_for_same_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            old = out / "old.md"
            old.parent.mkdir(parents=True)
            old.write_text("old", encoding="utf-8")
            stage = Path(tmp) / "stage"
            (stage / "new.md").parent.mkdir(parents=True)
            (stage / "new.md").write_text("new", encoding="utf-8")
            pdf_convert.promote_pdf_outputs(
                out,
                stage,
                {"doc.pdf": {"markdown": "old.md", "images": "old_images"}},
                {"doc.pdf": {"markdown": "new.md", "images": "new_images"}},
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
                '{"files":{"one.pdf":{"markdown":"one.md","images":"one_images"}}}',
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
            manifest.write_text('{"files":{"doc.pdf":{"markdown":"doc.md"}}}',
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
            manifest.write_text('{"files":{"doc.pdf":{"markdown":"doc.md"}}}',
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
            manifest.write_text('{"files":{"doc.pdf":{"markdown":"doc.md"}}}',
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
