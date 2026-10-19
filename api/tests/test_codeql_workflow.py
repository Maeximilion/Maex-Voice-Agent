"""Keep dependency updates inside the security scan's trust boundary."""

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


class CodeQLWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / ".github/workflows/codeql.yml").read_text(encoding="utf-8")
        self.workflow = yaml.safe_load(self.text)

    def test_dependency_prs_are_scanned_without_actor_or_path_exclusions(self):
        triggers = self.workflow["on"]
        self.assertEqual(triggers["pull_request"], {"branches": ["main"]})
        self.assertEqual(triggers["push"], {"branches": ["main"]})
        self.assertIn("schedule", triggers)
        self.assertIn("workflow_dispatch", triggers)
        self.assertNotIn("pull_request_target", triggers)
        job = self.workflow["jobs"]["analyze"]
        self.assertNotIn("if", job)
        self.assertNotIn("github.actor", self.text)
        self.assertEqual(
            set(job["strategy"]["matrix"]["language"]),
            {"actions", "javascript-typescript", "python"},
        )

    def test_untrusted_prs_use_hosted_runners_and_no_project_execution(self):
        job = self.workflow["jobs"]["analyze"]
        self.assertEqual(job["runs-on"], "ubuntu-24.04")
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertEqual(
            job["permissions"], {"contents": "read", "security-events": "write"}
        )
        self.assertNotIn("secrets.", self.text)
        self.assertNotIn("continue-on-error", self.text)
        steps = job["steps"]
        checkout = next(
            s for s in steps if s.get("uses", "").startswith("actions/checkout@")
        )
        self.assertFalse(checkout["with"]["persist-credentials"])
        init = next(
            s
            for s in steps
            if s.get("uses", "").startswith("github/codeql-action/init@")
        )
        self.assertEqual(init["with"]["build-mode"], "none")
        for step in steps:
            if "uses" in step:
                self.assertRegex(step["uses"], re.compile(r"@[0-9a-f]{40}$"))
            if "run" in step:
                self.assertNotRegex(step["run"], r"\b(pip|npm|make|python|bash)\b")

    def test_rollout_keeps_full_analysis_and_makes_validation_explicit(self):
        steps = self.workflow["jobs"]["analyze"]["steps"]
        analyze = next(
            s
            for s in steps
            if s.get("uses", "").startswith("github/codeql-action/analyze@")
        )
        self.assertNotIn("if", analyze)
        self.assertEqual(
            analyze["with"]["category"], "/language:${{ matrix.language }}"
        )
        self.assertEqual(
            analyze["with"]["upload"],
            "${{ env.CODEQL_PUBLISH == 'true' && 'always' || 'never' }}",
        )
        self.assertEqual(self.workflow["env"], {"CODEQL_PUBLISH": "false"})
        self.assertNotIn("vars.", self.text)
        self.assertIn("GITHUB_STEP_SUMMARY", self.text)
        self.assertIn("not published", self.text)


if __name__ == "__main__":
    unittest.main()
