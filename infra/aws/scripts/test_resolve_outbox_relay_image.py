"""通常applyでLambdaを消さないためのstate引き継ぎ契約。"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("resolve-outbox-relay-image.py")
CURRENT = "sha256:" + "a" * 64
PREVIOUS = "sha256:" + "b" * 64


def state_with_image(image):
    return {
        "resources": [
            {
                "mode": "managed",
                "type": "aws_lambda_function",
                "name": "outbox_relay",
                "instances": [{"attributes": {"image_uri": image}}],
            }
        ]
    }


class ResolveRelayImageTest(unittest.TestCase):
    def resolve(self, state, requested=""):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--digest", requested],
            input=json.dumps(state),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    def test_first_infrastructure_apply_has_no_function(self):
        result = self.resolve({"resources": []})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"outbox_relay_image_digest": None})

    def test_regular_apply_preserves_deployed_digest(self):
        result = self.resolve(state_with_image(f"example.invalid/backend@{CURRENT}"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["outbox_relay_image_digest"], CURRENT)

    def test_explicit_digest_can_roll_back(self):
        result = self.resolve(
            state_with_image(f"example.invalid/backend@{CURRENT}"), PREVIOUS
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["outbox_relay_image_digest"], PREVIOUS)

    def test_invalid_state_is_not_treated_as_first_apply(self):
        for state in ({}, state_with_image("example.invalid/backend:latest")):
            with self.subTest(state=state):
                result = self.resolve(state)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_mutable_tag_is_rejected(self):
        result = self.resolve({"resources": []}, "latest")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
