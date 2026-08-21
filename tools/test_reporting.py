import unittest

from reporting import Issue, Report


class ReportingTests(unittest.TestCase):
    def test_same_issue_catalog_renders_json_readme_and_console(self):
        report = Report()
        report.add(Issue("source_missing_link", "Missing link", "page.md"))
        report.add(Issue("missing_image", "Missing image", "page.md", fatal=True))
        data = report.as_dict()
        self.assertEqual(2, data["summary"]["total"])
        self.assertEqual(1, data["summary"]["fatal"])
        self.assertIn("Missing local image", report.to_readme())
        self.assertIn("FATAL", report.to_console())
        self.assertIn('"fatal": 1', report.to_json())
        self.assertIn('"label": "Missing local image"', report.to_json())
        self.assertIn("Missing local image", report.to_console())

    def test_console_keeps_all_fatals_and_bounds_advisory_detail(self):
        issues = [Issue("source_missing_link", f"advisory-{index}", "source.md")
                  for index in range(500)]
        issues.extend(Issue("replacement_character", f"fatal-{index}", f"page-{index}.md", True)
                      for index in range(4))
        report = Report(issues)
        console = report.to_console()
        self.assertLess(len(console), 4000)
        for index in range(4):
            self.assertIn(f"fatal-{index}", console)
        self.assertIn("advisories: 500 issue(s) in 1 kind(s)", console)
        self.assertNotIn("advisory-499", console)
        self.assertIn("author-report.json", console)


if __name__ == "__main__":
    unittest.main()
