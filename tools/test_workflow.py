"""Static guardrails for the publication workflow's safety contract."""

import unittest
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "markdown-export.yml"

# Contexts GitHub refuses to evaluate in jobs.<id>.env. Referencing one there is
# a load-time error ("Unrecognized named-value"), which fails the whole run
# before any job starts and produces no logs to diagnose it from.
JOB_ENV_FORBIDDEN_CONTEXTS = ("runner", "steps", "job", "env", "secrets")


class WorkflowGuardrailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.workflow = yaml.safe_load(cls.text)

    def test_job_env_avoids_contexts_github_rejects_at_load_time(self):
        for job_id, job in self.workflow["jobs"].items():
            for name, value in (job.get("env") or {}).items():
                for context in JOB_ENV_FORBIDDEN_CONTEXTS:
                    self.assertNotRegex(
                        str(value),
                        r"\$\{\{\s*" + context + r"\.",
                        f"{job_id}.env.{name} uses the {context} context, "
                        "which GitHub rejects in job-level env",
                    )

    def test_convert_repo_is_relative_to_the_directory_uv_runs_in(self):
        steps = self.workflow["jobs"]["validate"]["steps"]
        convert = next(step for step in steps if step.get("name") == "Convert")
        # "--directory src" changes uv's working directory, so a "--repo src"
        # here would resolve to src/src and fail before a single topic converts.
        self.assertIn("--directory src", convert["run"])
        self.assertIn("--repo .", convert["run"])
        self.assertNotIn("--repo src", convert["run"])

    def test_build_paths_are_resolved_from_runner_temp_in_a_step(self):
        steps = self.workflow["jobs"]["validate"]["steps"]
        resolve = steps[0]
        self.assertEqual("Resolve build paths", resolve["name"])
        for name in ("EXPORT_DIR", "WORK_DIR", "DIAGNOSTICS"):
            self.assertIn(f"{name}=${{RUNNER_TEMP}}", resolve["run"])
        self.assertIn('>> "$GITHUB_ENV"', resolve["run"])

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

    def test_validation_trigger_and_read_only_permissions(self):
        self.assertIn("pull_request:", self.text)
        self.assertIn("permissions:\n  contents: read", self.text)
        self.assertIn("validate:", self.text)
        self.assertLess(self.text.index("uv lock --check"), self.text.index("uv sync --frozen"))
        self.assertIn("uv sync --frozen", self.text)

    def test_actions_are_immutable_pins(self):
        for pin in (
            "actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09",
            "astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e",
            "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
            "actions/download-artifact@634f93cb2916e3fdff6788551b99b062d0335ce0",
        ):
            self.assertIn(pin, self.text)
        self.assertNotRegex(self.text, r"uses:\s+[^\s]+@v\d+")

    def test_pandoc_digest_is_checked_before_install(self):
        digest = "ce4ac48f48aa7eadc1f5dbdf3449a1739f188ecb8c5421c5adc070fe7479e567"
        self.assertIn(digest, self.text)
        self.assertIn("sha256sum --check --strict", self.text)
        self.assertLess(self.text.index("sha256sum --check --strict"), self.text.index("dpkg -i"))

    def test_publish_is_separate_write_job_after_validation(self):
        self.assertRegex(self.text, r"publish:\s*[\s\S]+?permissions:\s*\n\s+contents: write")
        self.assertIn("needs: validate", self.text)
        self.assertIn("actions/download-artifact@634f93cb2916e3fdff6788551b99b062d0335ce0", self.text)
        publish = self.text[self.text.index("publish:"):]
        self.assertNotIn("github.event_name == 'pull_request'", publish)

    def test_diagnostics_are_always_uploaded_and_summary_reads_external_report(self):
        self.assertIn("if: always()", self.text)
        self.assertIn("markdown-export-diagnostics.json", self.text)
        self.assertIn("if-no-files-found: ignore", self.text)
        self.assertIn("GITHUB_STEP_SUMMARY", self.text)

    def test_completed_export_preserves_hidden_files(self):
        completed = self.text[self.text.index("- name: Upload completed export"):]
        self.assertIn("include-hidden-files: true", completed)
        self.assertEqual(1, self.text.count("include-hidden-files: true"))

    def test_summary_uses_canonical_codes_and_issue_labels(self):
        self.assertIn('"source_missing_link"', self.text)
        self.assertIn('"source_missing_image"', self.text)
        self.assertIn('item.get("label",', self.text)


if __name__ == "__main__":
    unittest.main()
