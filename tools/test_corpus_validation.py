import tempfile
import unittest
from pathlib import Path

from corpus_validation import validate_corpus


class CorpusValidationTests(unittest.TestCase):
    def test_resolves_local_links_and_reports_fatal_exporter_errors(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "ok.md").write_text(
                "# One\n\n[ok](two.md) ![image](img.png)\n\n- parent\n  - child\n",
                encoding="utf-8",
            )
            (root / "two.md").write_text("# Two\n", encoding="utf-8")
            (root / "img.png").write_bytes(b"image")
            issues = validate_corpus(root)
        self.assertEqual([], issues)

    def test_missing_image_and_malformed_list_are_fatal(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "page.md").write_text(
                "# Page\n\n![missing](missing.png)\n- - literal\n",
                encoding="utf-8",
            )
            (root / "missing.md").write_text("# Target\n", encoding="utf-8")
            issues = validate_corpus(root)
        by_code = {item.code: item for item in issues}
        self.assertTrue(by_code["missing_image"].fatal)
        self.assertTrue(by_code["malformed_list"].fatal)

    def test_source_missing_link_is_advisory_and_duplicate_title_is_reported(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "one.md").write_text(
                "---\nsource: a.htm\n---\n# Same\n\n[old](missing.htm)\n",
                encoding="utf-8",
            )
            (root / "two.md").write_text("# Same\n", encoding="utf-8")
            issues = validate_corpus(root, advisory_links={("one.md", "missing.htm")})
        self.assertTrue(any(i.code == "missing_link" and not i.fatal for i in issues))
        self.assertTrue(any(i.code == "duplicate_title" for i in issues))

    def test_unallowlisted_link_is_fatal_even_when_source_metadata_exists(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "page.md").write_text(
                "---\nsource: page.htm\n---\n# Page\n\n[rewritten](missing.md)\n",
                encoding="utf-8",
            )
            issues = validate_corpus(root, advisory_links={("page.md", "other.md")})
        self.assertTrue(any(i.code == "missing_link" and i.fatal for i in issues))

    def test_external_anchors_title_attributes_and_escaped_brackets_are_ignored(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "page.md").write_text(
                '# Page\n\n[external](https://example.test/no.md) [anchor](#x) '
                '[label](missing.md "title") \\[literal\\]\n', encoding="utf-8"
            )
            (root / "missing.md").write_text("# Target\n", encoding="utf-8")
            issues = validate_corpus(root)
        self.assertFalse(any(i.code == "missing_link" for i in issues))

    def test_balances_parentheses_angle_targets_and_ignores_fenced_code(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "Export_(LIFT).md").write_text("# Target\n", encoding="utf-8")
            (root / "space name.md").write_text("# Space\n", encoding="utf-8")
            (root / "page.md").write_text(
                "# Page\n\n[paren](Export_(LIFT).md) [angle](<space name.md>)\n"
                "```\n[ignored](nope.md)\n```\n", encoding="utf-8"
            )
            issues = validate_corpus(root)
        self.assertFalse(any(i.code == "missing_link" for i in issues))

    def test_rejects_a_local_target_that_escapes_the_corpus_root(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "page.md").write_text("# Page\n\n[out](../outside.md)\n", encoding="utf-8")
            issues = validate_corpus(root)
        self.assertTrue(any(i.code == "missing_link" and i.fatal for i in issues))

    def test_source_replacement_character_is_advisory_only_when_allowlisted(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "source.md").write_text("# Source\n\ufffd\n", encoding="utf-8")
            (root / "generated.md").write_text("# Generated\n\ufffd\n", encoding="utf-8")
            issues = validate_corpus(root, source_replacement_paths={"source.md"})
        by_path = {issue.path: issue for issue in issues if issue.code == "replacement_character"}
        self.assertFalse(by_path["source.md"].fatal)
        self.assertTrue(by_path["generated.md"].fatal)

    def test_duplicate_titles_are_case_insensitive(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "one.md").write_text("# Same\n", encoding="utf-8")
            (root / "two.md").write_text("# same\n", encoding="utf-8")
            issues = validate_corpus(root)
        self.assertTrue(any(issue.code == "duplicate_title" for issue in issues))

    def test_allowlisted_source_image_is_advisory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "page.md").write_text("# Page\n\n![source](missing.png)\n", encoding="utf-8")
            issues = validate_corpus(root, advisory_images={("page.md", "missing.png")})
        self.assertTrue(any(issue.code == "missing_image" and not issue.fatal for issue in issues))


if __name__ == "__main__":
    unittest.main()
