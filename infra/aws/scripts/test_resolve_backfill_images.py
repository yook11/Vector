"""backfillの初回配置と工程別の状態引き継ぎを検証する。"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("resolve-backfill-images.py")
CURATION = "sha256:" + "a" * 64
ASSESSMENT = "sha256:" + "b" * 64
EMBEDDING = "sha256:" + "c" * 64
UPDATED = "sha256:" + "d" * 64


def function(stage, digest):
    return {
        "index_key": stage,
        "attributes": {"image_uri": f"example.invalid/backend@{digest}"},
    }


def schedule(stage, state):
    return {"index_key": stage, "attributes": {"state": state}}


def state_with(functions, schedules):
    return {
        "resources": [
            {
                "mode": "managed",
                "type": "aws_lambda_function",
                "name": "backfill",
                "instances": functions,
            },
            {
                "mode": "managed",
                "type": "aws_scheduler_schedule",
                "name": "backfill",
                "instances": schedules,
            },
        ]
    }


def deployed_state():
    return state_with(
        [
            function("curation", CURATION),
            function("assessment", ASSESSMENT),
            function("embedding", EMBEDDING),
        ],
        [
            schedule("curation", "ENABLED"),
            schedule("assessment", "DISABLED"),
            schedule("embedding", "ENABLED"),
        ],
    )


class ResolveBackfillImagesTest(unittest.TestCase):
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

    def assert_rejected(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("example.invalid", result.stderr)

    def test_empty_state_without_images_keeps_all_stages_absent(self):
        self.assertEqual(
            self.configuration(self.resolve({"resources": []})),
            {
                "curation_backfill_image_digest": None,
                "curation_backfill_enabled": False,
                "assessment_backfill_image_digest": None,
                "assessment_backfill_enabled": False,
                "embedding_backfill_image_digest": None,
                "embedding_backfill_enabled": False,
            },
        )

    def test_first_deployment_enables_each_supplied_image(self):
        self.assertEqual(
            self.configuration(
                self.resolve(
                    {"resources": []},
                    "--curation-digest",
                    CURATION,
                    "--assessment-digest",
                    ASSESSMENT,
                    "--embedding-digest",
                    EMBEDDING,
                )
            ),
            {
                "curation_backfill_image_digest": CURATION,
                "curation_backfill_enabled": True,
                "assessment_backfill_image_digest": ASSESSMENT,
                "assessment_backfill_enabled": True,
                "embedding_backfill_image_digest": EMBEDDING,
                "embedding_backfill_enabled": True,
            },
        )

    def test_regular_update_preserves_images_and_disabled_assessment(self):
        self.assertEqual(
            self.configuration(self.resolve(deployed_state())),
            {
                "curation_backfill_image_digest": CURATION,
                "curation_backfill_enabled": True,
                "assessment_backfill_image_digest": ASSESSMENT,
                "assessment_backfill_enabled": False,
                "embedding_backfill_image_digest": EMBEDDING,
                "embedding_backfill_enabled": True,
            },
        )

    def test_explicit_assessment_image_update_preserves_stopped_schedule(self):
        config = self.configuration(
            self.resolve(deployed_state(), "--assessment-digest", UPDATED)
        )
        self.assertEqual(config["assessment_backfill_image_digest"], UPDATED)
        self.assertFalse(config["assessment_backfill_enabled"])
        self.assertEqual(config["curation_backfill_image_digest"], CURATION)
        self.assertEqual(config["embedding_backfill_image_digest"], EMBEDDING)

    def test_explicit_curation_stop_preserves_image(self):
        config = self.configuration(
            self.resolve(deployed_state(), "--curation-state", "disabled")
        )
        self.assertFalse(config["curation_backfill_enabled"])
        self.assertEqual(config["curation_backfill_image_digest"], CURATION)
        self.assertTrue(config["embedding_backfill_enabled"])

    def test_explicit_assessment_start_uses_existing_image(self):
        config = self.configuration(
            self.resolve(deployed_state(), "--assessment-state", "enabled")
        )
        self.assertTrue(config["assessment_backfill_enabled"])
        self.assertEqual(config["assessment_backfill_image_digest"], ASSESSMENT)

    def test_first_embedding_deployment_can_be_explicitly_stopped(self):
        config = self.configuration(
            self.resolve(
                {"resources": []},
                "--embedding-digest",
                EMBEDDING,
                "--embedding-state",
                "disabled",
            )
        )
        self.assertEqual(config["embedding_backfill_image_digest"], EMBEDDING)
        self.assertFalse(config["embedding_backfill_enabled"])
        self.assertIsNone(config["curation_backfill_image_digest"])

    def test_partial_apply_with_function_only_creates_enabled_schedule(self):
        config = self.configuration(
            self.resolve(state_with([function("curation", CURATION)], []))
        )
        self.assertEqual(config["curation_backfill_image_digest"], CURATION)
        self.assertTrue(config["curation_backfill_enabled"])

    def test_deposed_instance_does_not_override_current_image(self):
        state = deployed_state()
        old = function("curation", UPDATED)
        old["deposed"] = "old-instance"
        state["resources"][0]["instances"].append(old)
        config = self.configuration(self.resolve(state))
        self.assertEqual(config["curation_backfill_image_digest"], CURATION)

    def test_child_module_does_not_override_root_schedule(self):
        state = deployed_state()
        child = state_with([], [schedule("curation", "DISABLED")])["resources"][1]
        child["module"] = "module.unrelated"
        state["resources"].append(child)
        self.assertTrue(
            self.configuration(self.resolve(state))["curation_backfill_enabled"]
        )

    def test_unrelated_relay_does_not_supply_a_backfill_image(self):
        state = state_with([function("curation", CURATION)], [])
        state["resources"][0]["name"] = "curation_outbox_relay"
        self.assertIsNone(
            self.configuration(self.resolve(state))["curation_backfill_image_digest"]
        )

    def test_explicit_enable_without_image_is_rejected(self):
        self.assert_rejected(
            self.resolve({"resources": []}, "--assessment-state", "enabled")
        )

    def test_invalid_requested_digest_is_rejected(self):
        for digest in ("latest", "sha256:abc", "sha256:" + "A" * 64):
            with self.subTest(digest=digest):
                self.assert_rejected(
                    self.resolve(deployed_state(), "--embedding-digest", digest)
                )

    def test_invalid_state_container_is_not_treated_as_absent(self):
        for state in ({}, [], {"resources": None}, {"resources": [{}]}):
            with self.subTest(state=state):
                self.assert_rejected(self.resolve(state))

    def test_invalid_current_image_is_rejected_even_with_override(self):
        state = state_with([function("curation", "latest")], [])
        self.assert_rejected(self.resolve(state, "--curation-digest", CURATION))

    def test_invalid_schedule_state_is_rejected_even_with_stop(self):
        state = state_with(
            [function("assessment", ASSESSMENT)], [schedule("assessment", "unknown")]
        )
        self.assert_rejected(self.resolve(state, "--assessment-state", "disabled"))

    def test_schedule_without_function_is_rejected(self):
        self.assert_rejected(
            self.resolve(state_with([], [schedule("curation", "DISABLED")]))
        )

    def test_duplicate_function_stage_is_rejected(self):
        state = state_with(
            [function("curation", CURATION), function("curation", UPDATED)], []
        )
        self.assert_rejected(self.resolve(state))

    def test_duplicate_schedule_stage_is_rejected(self):
        state = state_with(
            [function("curation", CURATION)],
            [schedule("curation", "ENABLED"), schedule("curation", "DISABLED")],
        )
        self.assert_rejected(self.resolve(state))

    def test_unknown_stage_key_is_rejected(self):
        self.assert_rejected(
            self.resolve(state_with([function("other", CURATION)], []))
        )

    def test_missing_stage_key_is_rejected(self):
        instance = function("curation", CURATION)
        del instance["index_key"]
        self.assert_rejected(self.resolve(state_with([instance], [])))


if __name__ == "__main__":
    unittest.main()
