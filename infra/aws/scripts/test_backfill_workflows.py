"""CIの実shellをスタブで実行し、設定の配置と失敗時の停止を確認する。"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from test_resolve_backfill_images import CURATION, UPDATED, deployed_state

ROOT = Path(__file__).resolve().parents[3]


def run_block(workflow, step_name):
    source = (ROOT / ".github/workflows" / workflow).read_text()
    step = source.split(f"      - name: {step_name}\n", 1)[1]
    lines = step.split("        run: |\n", 1)[1].splitlines()
    commands = []
    for line in lines:
        if line and not line.startswith("          "):
            break
        commands.append(line)
    return textwrap.dedent("\n".join(commands))


class WorkflowSandbox(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        scripts = self.directory / "scripts"
        scripts.mkdir()
        shutil.copy(Path(__file__).with_name(self.script), scripts)
        (self.directory / "state.json").write_text(json.dumps(self.initial_state()))
        self.env = {
            "PATH": f"{self.bin}:{os.defpath}",
            "GITHUB_STEP_SUMMARY": str(self.directory / "summary"),
            **{name: "" for name in self.input_names},
        }
        self.executable("terraform", "#!/bin/sh\ncat state.json\n")

    def executable(self, name, source):
        path = self.bin / name
        path.write_text(source)
        path.chmod(0o755)

    def execute(self, workflow, step):
        return subprocess.run(
            ["/bin/bash", "-c", run_block(workflow, step)],
            cwd=self.directory,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def apply(self):
        return self.execute("aws-terraform-apply.yml", self.apply_step)

    def plan(self):
        return self.execute(
            "aws-terraform-plan.yml",
            self.plan_step,
        )

    def settings(self):
        return json.loads((self.directory / self.settings_file).read_text())

    def assert_no_settings(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.directory / self.settings_file).exists())
        self.assertEqual(list(self.directory.glob(self.temporary_pattern)), [])

    def ecr_stubs(self):
        self.executable(
            "terraform",
            '#!/bin/sh\nif [ "$1" = state ]; then cat state.json; else\n'
            "printf '%s\\n' '{\"backend\":\"registry.invalid/vector/backend\"}'\nfi\n",
        )
        (self.bin / "jq").symlink_to(shutil.which("jq"))


class BackfillWorkflowTest(WorkflowSandbox):
    script = "resolve-backfill-images.py"
    initial_state = staticmethod(deployed_state)
    input_names = (
        "REQUESTED_CURATION_DIGEST",
        "REQUESTED_ASSESSMENT_DIGEST",
        "REQUESTED_EMBEDDING_DIGEST",
    )
    apply_step = "Resolve Backfill images and schedules"
    plan_step = "Preserve Backfill images and schedules from state"
    settings_file = "backfill.auto.tfvars.json"
    temporary_pattern = "backfill-vars.*"

    def test_plan_preserves_stopped_assessment(self):
        result = self.plan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.settings()["assessment_backfill_enabled"])
        self.assertEqual(self.settings()["curation_backfill_image_digest"], CURATION)

    def test_apply_without_inputs_preserves_stopped_assessment(self):
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.settings()["assessment_backfill_enabled"])
        self.assertEqual(self.settings()["curation_backfill_image_digest"], CURATION)

    def test_plan_state_pull_failure_does_not_install_even_valid_partial_output(self):
        self.executable("terraform", "#!/bin/sh\ncat state.json\nexit 42\n")
        self.assert_no_settings(self.plan())

    def test_apply_state_pull_failure_does_not_install_settings(self):
        self.executable("terraform", "#!/bin/sh\ncat state.json\nexit 42\n")
        self.assert_no_settings(self.apply())

    def test_apply_explicit_stop_is_forwarded_to_resolver(self):
        self.env["REQUESTED_CURATION_STATE"] = "disabled"
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.settings()["curation_backfill_enabled"])
        self.assertTrue(self.settings()["embedding_backfill_enabled"])

    def test_apply_missing_requested_image_stops_before_installing_settings(self):
        self.ecr_stubs()
        self.env["REQUESTED_CURATION_DIGEST"] = UPDATED
        self.executable("aws", "#!/bin/sh\nprintf '%s\\n' None\n")
        self.assert_no_settings(self.apply())

    def test_apply_existing_requested_image_is_installed(self):
        self.ecr_stubs()
        self.env["REQUESTED_CURATION_DIGEST"] = UPDATED
        self.executable("aws", f"#!/bin/sh\nprintf '%s\\n' '{UPDATED}'\n")
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.settings()["curation_backfill_image_digest"], UPDATED)
        self.assertFalse(self.settings()["assessment_backfill_enabled"])


if __name__ == "__main__":
    unittest.main()
