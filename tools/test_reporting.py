import unittest

from reporting import Issue, Report


class ReportingTests(unittest.TestCase):
    def test_markdown_report_gives_robohelp_authors_paths_evidence_and_guidance(self):
        report = Report([
            Issue(
                "source_missing_link",
                "missing | target\nsecond line",
                "Using_Tools/topic.htm",
                detail=["Using_Tools/topic.htm", "missing.htm"],
            ),
        ], metadata={
            "source_ref": "abc1234",
            "chm_count": 2,
            "topic_count": 1630,
            "image_count": 583,
            "pdf_count": 13,
        })

        markdown = report.to_markdown()

        self.assertIn("# Author quality report", markdown)
        self.assertIn("`abc1234`", markdown)
        self.assertIn("| CHMs | 2 |", markdown)
        self.assertIn("## Missing local link (`source_missing_link`)", markdown)
        self.assertIn("**How to fix in RoboHelp:**", markdown)
        self.assertIn("Open the source topic", markdown)
        self.assertIn("`Using_Tools/topic.htm`", markdown)
        self.assertIn("missing \\| target<br>second line", markdown)
        self.assertIn('&#91;"Using_Tools/topic.htm", "missing.htm"&#93;', markdown)
        self.assertIn("[author-report.json](author-report.json)", markdown)

    def test_markdown_report_neutralizes_source_markdown_and_backslashes(self):
        report = Report([
            Issue(
                "source_missing_link",
                "[click](javascript:alert(1)) \\| raw `text`",
                "topic[1].htm",
                detail={"target": "[bad](missing.htm)"},
            ),
        ])

        markdown = report.to_markdown()

        self.assertNotIn("[click](javascript:alert(1))", markdown)
        self.assertNotIn("[bad](missing.htm)", markdown)
        self.assertIn("&#91;click&#93;(javascript:alert(1))", markdown)
        self.assertIn("&#92;\\| raw &#96;text&#96;", markdown)
        self.assertIn("`topic&#91;1&#93;.htm`", markdown)

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
