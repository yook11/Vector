import json
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("resolve-auth-rate-limit-cleanup.py")
DIGEST = "sha256:" + "a" * 64
UPDATED = "sha256:" + "b" * 64


def resource(resource_type, attributes, *, deposed=None):
    instance = {"attributes": attributes}
    if deposed is not None:
        instance["deposed"] = deposed
    return {
        "mode": "managed",
        "type": resource_type,
        "name": "auth_rate_limit_cleanup",
        "instances": [instance],
    }


def deployed_state(schedule_state="DISABLED"):
    return {
        "resources": [
            resource(
                "aws_lambda_function",
                {"image_uri": f"example.invalid/backend@{DIGEST}"},
            ),
            resource("aws_scheduler_schedule", {"state": schedule_state}),
        ]
    }


class ResolveAuthRateLimitCleanupTest(unittest.TestCase):
    def resolve(self, state, *arguments):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            input=json.dumps(state),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    def configuration(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_first_deployment_with_image_remains_disabled(self):
        config = self.configuration(self.resolve({"resources": []}, "--digest", DIGEST))
        self.assertEqual(config["auth_rate_limit_cleanup_image_digest"], DIGEST)
        self.assertFalse(config["auth_rate_limit_cleanup_enabled"])

    def test_regular_run_preserves_digest_and_disabled_schedule(self):
        self.assertEqual(
            self.configuration(self.resolve(deployed_state())),
            {
                "auth_rate_limit_cleanup_image_digest": DIGEST,
                "auth_rate_limit_cleanup_enabled": False,
            },
        )

    def test_explicit_enable_uses_existing_image(self):
        config = self.configuration(
            self.resolve(deployed_state(), "--state", "enabled")
        )
        self.assertTrue(config["auth_rate_limit_cleanup_enabled"])
        self.assertEqual(config["auth_rate_limit_cleanup_image_digest"], DIGEST)

    def test_image_update_preserves_enabled_schedule(self):
        config = self.configuration(
            self.resolve(deployed_state("ENABLED"), "--digest", UPDATED)
        )
        self.assertTrue(config["auth_rate_limit_cleanup_enabled"])
        self.assertEqual(config["auth_rate_limit_cleanup_image_digest"], UPDATED)

    def test_enable_without_image_is_rejected(self):
        result = self.resolve({"resources": []}, "--state", "enabled")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_invalid_current_state_is_rejected_without_leaking_image(self):
        result = self.resolve(deployed_state("UNKNOWN"))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("example.invalid", result.stderr)


if __name__ == "__main__":
    unittest.main()
