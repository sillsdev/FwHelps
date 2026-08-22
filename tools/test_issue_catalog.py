import unittest

from issue_catalog import ISSUE_ALIASES, ISSUE_CATALOG, IssuePolicy
from reporting import LABELS, Issue, make_issue


class IssueCatalogTests(unittest.TestCase):
    def test_every_producer_alias_resolves_to_one_catalog_policy(self):
        self.assertTrue(ISSUE_ALIASES)
        for code in ISSUE_ALIASES.values():
            self.assertIn(code, ISSUE_CATALOG)

    def test_every_emitted_issue_code_has_exactly_one_policy(self):
        emitted = {
            "missing_link", "source_missing_link", "missing_image", "source_missing_image",
            "duplicate_title", "malformed_list", "replacement_character",
            "source_replacement_character", "raw_html", "one_h1", "destination_collision",
            "unsafe_uri", "path_escape", "source_unsafe_uri", "source_path_escape",
            "pandoc_failure", "unmapped_span", "pdf_failure",
            "outline_drift", "outline_unpinned", "stale_toc_entries", "not_in_toc",
            "chm_failure", "chm_discovery", "unknown_issue",
        }
        self.assertEqual(emitted, set(ISSUE_CATALOG))

    def test_policies_are_immutable_and_cover_representative_issues(self):
        self.assertIsInstance(ISSUE_CATALOG["pdf_failure"], IssuePolicy)
        self.assertTrue(all(policy.guidance.strip() for policy in ISSUE_CATALOG.values()))
        self.assertTrue(ISSUE_CATALOG["pdf_failure"].fatal)
        self.assertEqual("exporter", ISSUE_CATALOG["pdf_failure"].provenance)
        self.assertFalse(ISSUE_CATALOG["raw_html"].fatal)
        self.assertEqual("source", ISSUE_CATALOG["raw_html"].provenance)
        with self.assertRaises((AttributeError, TypeError)):
            ISSUE_CATALOG["raw_html"].fatal = True
        with self.assertRaises(TypeError):
            LABELS["raw_html"] = "changed"

    def test_public_policy_constructor_remains_source_compatible(self):
        policy = IssuePolicy("Example", False, "source")
        self.assertEqual("", policy.guidance)

    def test_make_issue_uses_catalog_policy(self):
        issue = make_issue("html_tables_kept", "table kept", "doc.pdf")
        self.assertEqual("raw_html", issue.code)
        self.assertEqual("Raw HTML retained", issue.label)
        self.assertFalse(issue.fatal)
        self.assertEqual("source", issue.provenance)

    def test_unknown_issue_is_fatal_and_does_not_raise_key_error(self):
        issue = make_issue("future_producer_code", "new failure", "page.md")
        self.assertEqual("unknown_issue", issue.code)
        self.assertTrue(issue.fatal)
        self.assertEqual("exporter", issue.provenance)
        self.assertIn("future_producer_code", issue.message)
        self.assertTrue(Issue("another_future_code", "failure", fatal=False).fatal)

    def test_legacy_severity_arguments_cannot_override_catalog_policy(self):
        issue = Issue("missing_link", "generated missing link", fatal=False, provenance="source")
        self.assertTrue(issue.fatal)
        self.assertEqual("exporter", issue.provenance)

    def test_direct_issue_also_gets_catalog_defaults(self):
        issue = Issue("outline_drift", "changed", "guide.pdf")
        self.assertTrue(issue.fatal)
        self.assertEqual("PDF outline drift", issue.label)


if __name__ == "__main__":
    unittest.main()
