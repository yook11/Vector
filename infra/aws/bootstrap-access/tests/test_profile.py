"""bootstrap用CLIプロファイルの本人確認契約。"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify-aws-profile.sh"
ACCOUNT = "123456789012"


class BootstrapProfileTest(unittest.TestCase):
    def run_profile(self, profile, role, account=ACCOUNT):
        with tempfile.TemporaryDirectory() as directory:
            aws = Path(directory) / "aws"
            aws.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
case "$1 $2" in
  'configure list-profiles')
    printf '%s\\n' vector-bootstrap vector-bootstrap-apply
    ;;
  'configure get')
    case "$3" in
      sso_account_id) echo 123456789012 ;;
      role_arn) echo arn:aws:iam::123456789012:role/vector-bootstrap-apply ;;
      *) exit 2 ;;
    esac
    ;;
  'sts get-caller-identity')
    printf '%s\\tarn:aws:sts::%s:assumed-role/%s/test-session\\n' "$TEST_ACCOUNT" "$TEST_ACCOUNT" "$TEST_ROLE"
    ;;
  *) exit 2 ;;
esac
"""
            )
            aws.chmod(0o755)
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("AWS_")
            }
            environment.update(
                PATH=f"{directory}:{environment['PATH']}",
                TEST_ROLE=role,
                TEST_ACCOUNT=account,
            )
            return subprocess.run(
                ["bash", str(SCRIPT), profile],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

    def test_entry_and_executor_are_accepted_separately(self):
        for profile, role in [
            ("vector-bootstrap", "AWSReservedSSO_VectorBootstrap_abc123"),
            ("vector-bootstrap-apply", "vector-bootstrap-apply"),
        ]:
            with self.subTest(profile=profile):
                result = self.run_profile(profile, role)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn(ACCOUNT, result.stdout)

    def test_other_roles_cannot_be_used_as_bootstrap(self):
        for profile in ["vector-bootstrap", "vector-bootstrap-apply"]:
            for role in [
                "AWSReservedSSO_WorkloadAdministrator_abc123",
                "AWSReservedSSO_VectorDeploy_abc123",
                "AWSReservedSSO_VectorTestManager_abc123",
                "AWSReservedSSO_VectorBootstrapExtra_abc123",
            ]:
                with self.subTest(profile=profile, role=role):
                    result = self.run_profile(profile, role)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("caller roleが期待値と一致しません", result.stderr)
                    self.assertIn(
                        "aws sso login --profile vector-bootstrap", result.stderr
                    )

    def test_another_account_is_rejected(self):
        result = self.run_profile(
            "vector-bootstrap-apply", "vector-bootstrap-apply", "111111111111"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("caller accountが設定と一致しません", result.stderr)


if __name__ == "__main__":
    unittest.main()
