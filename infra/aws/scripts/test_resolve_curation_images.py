"""Curationの配置・更新で、両Lambdaのdigestを原子的に解決する。"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("resolve-curation-images.py")
CONSUMER = "sha256:" + "a" * 64
RELAY = "sha256:" + "b" * 64
PREVIOUS = "sha256:" + "c" * 64


def function(name, digest):
    return {
        "mode": "managed",
        "type": "aws_lambda_function",
        "name": name,
        "instances": [
            {"attributes": {"image_uri": f"example.invalid/backend@{digest}"}}
        ],
    }


def deployed_state():
    return {
        "resources": [
            function("curation_consumer", CONSUMER),
            function("curation_outbox_relay", RELAY),
            function("assessment_consumer", PREVIOUS),
        ]
    }


class ResolveCurationImagesTest(unittest.TestCase):
    def resolve(self, state, consumer="", relay=""):
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--consumer-digest",
                consumer,
                "--relay-digest",
                relay,
            ],
            input=json.dumps(state),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    def assert_digests(self, result, consumer, relay):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "curation_consumer_image_digest": consumer,
                "curation_outbox_relay_image_digest": relay,
            },
        )

    def test_first_setup_and_consumer_before_relay(self):
        state = {"resources": [function("assessment_consumer", PREVIOUS)]}
        self.assert_digests(self.resolve(state), None, None)
        self.assert_digests(self.resolve(state, consumer=CONSUMER), CONSUMER, None)
        state["resources"].append(function("curation_consumer", CONSUMER))
        self.assert_digests(self.resolve(state), CONSUMER, None)
        self.assert_digests(self.resolve(state, relay=RELAY), CONSUMER, RELAY)

    def test_regular_apply_preserves_both_current_images(self):
        self.assert_digests(self.resolve(deployed_state()), CONSUMER, RELAY)

    def test_explicit_update_or_rollback_changes_only_selected_function(self):
        for consumer, relay, expected in [
            (PREVIOUS, "", (PREVIOUS, RELAY)),
            ("", PREVIOUS, (CONSUMER, PREVIOUS)),
            (PREVIOUS, PREVIOUS, (PREVIOUS, PREVIOUS)),
        ]:
            with self.subTest(consumer=consumer, relay=relay):
                self.assert_digests(
                    self.resolve(deployed_state(), consumer, relay), *expected
                )

    def test_deposed_and_module_instances_do_not_replace_root_current(self):
        state = deployed_state()
        state["resources"][0]["instances"].append(
            {"deposed": "old", "attributes": {"image_uri": "old:tag"}}
        )
        child = function("curation_consumer", PREVIOUS)
        child["module"] = "module.unrelated"
        state["resources"].append(child)
        self.assert_digests(self.resolve(state), CONSUMER, RELAY)

    def test_invalid_either_side_produces_no_partial_json_even_with_override(self):
        invalid_states = [
            {},
            [],
            {"resources": None},
            {"resources": [None]},
            {"resources": [{}]},
        ]
        for field, value in [("mode", "unknown"), ("module", 1), ("instances", None)]:
            state = deployed_state()
            state["resources"][1][field] = value
            invalid_states.append(state)
        for name in ("curation_consumer", "curation_outbox_relay"):
            for image in ("backend:latest", "backend@sha256:bad", None):
                state = deployed_state()
                target = next(r for r in state["resources"] if r["name"] == name)
                target["instances"][0]["attributes"]["image_uri"] = image
                invalid_states.append(state)
            state = deployed_state()
            state["resources"].append(function(name, PREVIOUS))
            invalid_states.append(state)
        for state in invalid_states:
            with self.subTest(state=state):
                result = self.resolve(state, CONSUMER, RELAY)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")

    def test_invalid_requested_digest_never_produces_partial_configuration(self):
        for bad in ("latest", "null", "sha256:" + "A" * 64, "sha256:abc"):
            for consumer, relay in ((bad, ""), (CONSUMER, bad)):
                with self.subTest(consumer=consumer, relay=relay):
                    result = self.resolve(deployed_state(), consumer, relay)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
