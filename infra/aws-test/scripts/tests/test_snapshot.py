"""AWS試験のスナップショットが共通の宛先レンジを同梱することを検証する。"""

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from smoke_runner import common, snapshot  # noqa: E402


class SnapshotDestinationPolicyTests(unittest.TestCase):
    def test_saved_terraform_resolves_the_copied_range_source(self):
        """保存されたTerraformの相対参照が、正本と同じ内容のJSONに到達する。"""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            local = directory / "local"
            common.save(
                local / "account.json",
                {
                    "expected_account_id": "123456789012",
                    "smoke_aws_profile": "manager",
                },
            )
            common.save(
                local / "images.tfvars.json",
                {
                    "source_revision": "a" * 40,
                    "backend_image_digest": "sha256:" + "b" * 64,
                    "proxy_image_digest": "sha256:" + "c" * 64,
                },
            )
            home_directory = directory / "home"
            aws_directory = home_directory / ".aws"
            aws_directory.mkdir(parents=True)
            (aws_directory / "config").write_text(
                "[profile manager]\n"
                "sso_account_id = 123456789012\n"
                "sso_role_name = VectorTestManager\n"
                "sso_session = test\n"
                "[profile runner]\n"
                "sso_account_id = 123456789012\n"
                "sso_role_name = VectorTestRunner\n"
                "sso_session = test\n"
                "[sso-session test]\n"
                "sso_start_url = https://example.invalid/start\n"
                "sso_region = ap-northeast-1\n"
            )
            run = directory / "runs" / "sample"
            with (
                patch.object(snapshot, "LOCAL", local),
                patch.object(Path, "home", return_value=home_directory),
            ):
                snapshot.create(run, "sample", "runner")

            workspace = run / "workspace"
            terraform = workspace / "infra/aws-test/smoke/compute.tf"
            references = re.findall(
                r'file\("\$\{path.module\}([^"\n]+/non_public_ranges\.json)"\)',
                terraform.read_text(),
            )
            self.assertEqual(len(references), 1)
            copied = (terraform.parent / references[0].lstrip("/")).resolve()
            relative = "backend/app/http/non_public_ranges.json"
            self.assertEqual(copied, (workspace / relative).resolve())
            self.assertEqual(
                copied.read_bytes(), (snapshot.ROOT / relative).read_bytes()
            )
            self.assertIn(f"workspace/{relative}", snapshot.verify(run)["files"])
