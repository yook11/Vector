"""投入Lambdaの初回停止と既存予定回の稼働状態をCLI境界で確認する。"""

import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("resolve-acquisition-consumer.py")
CURRENT = "sha256:" + "a" * 64
UPDATED = "sha256:" + "b" * 64


def deployed_state(enabled=True):
    return {
        "resources": [
            {
                "mode": "managed",
                "type": "aws_lambda_function",
                "name": "acquisition_consumer",
                "instances": [
                    {
                        "index_key": 0,
                        "attributes": {
                            "image_uri": f"example.invalid/backend@{CURRENT}"
                        },
                    }
                ],
            },
            {
                "mode": "managed",
                "type": "aws_lambda_event_source_mapping",
                "name": "acquisition_consumer",
                "instances": [
                    {
                        "index_key": 0,
                        "attributes": {"enabled": enabled},
                    }
                ],
            },
        ]
    }


class ResolveAcquisitionConsumerTest(unittest.TestCase):
    def resolve(self, state, *arguments):
        # 固定したローカルスクリプトをシェルを介さず検証する。
        return subprocess.run(  # noqa: S603
            [sys.executable, str(SCRIPT), *arguments],
            input=json.dumps(state),
            capture_output=True,
            text=True,
            timeout=10,
        )

    def settings(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_initial_deployment_needs_explicit_enable(self):
        for arguments, digest in [((), None), (("--digest", CURRENT), CURRENT)]:
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    self.settings(self.resolve({"resources": []}, *arguments)),
                    {
                        "acquisition_consumer_image_digest": digest,
                        "acquisition_consumer_enabled": False,
                    },
                )

    def test_keep_preserves_running_and_stopped_schedules(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.assertEqual(
                    self.settings(self.resolve(deployed_state(enabled))),
                    {
                        "acquisition_consumer_image_digest": CURRENT,
                        "acquisition_consumer_enabled": enabled,
                    },
                )

    def test_update_image_does_not_start_stopped_schedules(self):
        self.assertEqual(
            self.settings(self.resolve(deployed_state(False), "--digest", UPDATED)),
            {
                "acquisition_consumer_image_digest": UPDATED,
                "acquisition_consumer_enabled": False,
            },
        )

    def test_explicit_state_changes(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                result = self.resolve(
                    deployed_state(not enabled),
                    "--state",
                    "enabled" if enabled else "disabled",
                )
                self.assertEqual(
                    self.settings(result)["acquisition_consumer_enabled"], enabled
                )

    def test_invalid_or_inconsistent_state_never_emits_configuration(self):
        partial = deployed_state()
        partial["resources"][1]["instances"] *= 2
        mixed = deployed_state()
        mixed["resources"][1]["instances"][0]["attributes"]["enabled"] = "false"
        duplicate = deployed_state()
        duplicate["resources"][0]["instances"] *= 2
        orphan = deployed_state()
        orphan["resources"][0]["instances"] = []
        malformed = deployed_state()
        malformed["resources"][0]["instances"][0]["attributes"]["image_uri"] = (
            "example.invalid/secret:latest"
        )
        for state in (
            {},
            {"resources": [None]},
            partial,
            mixed,
            duplicate,
            orphan,
            malformed,
        ):
            with self.subTest(state=state):
                result = self.resolve(state)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("example.invalid", result.stderr)

    def test_invalid_digest_and_enable_without_image_are_rejected(self):
        for args in (("--digest", "latest"), ("--state", "enabled")):
            with self.subTest(args=args):
                result = self.resolve({"resources": []}, *args)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_deposed_instance_does_not_replace_current_image(self):
        state = deployed_state()
        old = copy.deepcopy(state["resources"][0]["instances"][0])
        old["deposed"] = "old"
        old["attributes"]["image_uri"] = "retired"
        state["resources"][0]["instances"].append(old)
        self.assertEqual(
            self.settings(self.resolve(state))["acquisition_consumer_image_digest"],
            CURRENT,
        )


if __name__ == "__main__":
    unittest.main()
