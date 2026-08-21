"""Static guardrails for the publication workflow's safety contract."""

import unittest
from pathlib import Path

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "markdown-export.yml"


class WorkflowGuardrailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_summary_consumes_canonical_report_schema(self):
        self.assertIn('r.get("corpus", {})', self.text)
        self.assertIn('r.get("summary", {})', self.text)
        self.assertIn('r.get("issues", [])', self.text)
        self.assertNotIn("r.get('topics',0)", self.text)

    def test_publication_removes_tracked_stale_files(self):
        self.assertIn("git -C \"${PUBLISH_DIR}\" rm -r -q --ignore-unmatch -- .", self.text)
        self.assertIn('git -C "${PUBLISH_DIR}" clean -fdx -q', self.text)

    def test_publication_keeps_history_and_never_overwrites(self):
        self.assertIn("refs/heads/${EXPORT_BRANCH}:refs/remotes/origin/${EXPORT_BRANCH}", self.text)
        self.assertNotIn("push --force", self.text)
        self.assertNotIn("push --force-with-lease", self.text)
        self.assertIn('git push origin "refs/tags/${TAG}"', self.text)

    def test_dry_run_does_not_attempt_release_tagging(self):
        self.assertIn("github.event_name != 'workflow_dispatch' || !inputs.dry_run", self.text)


if __name__ == "__main__":
    unittest.main()
