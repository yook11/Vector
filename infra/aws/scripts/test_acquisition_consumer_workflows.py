"""既存と同じ配備shell検証で、取得Consumerの設定を安全に配置することを確認する。"""

from test_backfill_workflows import WorkflowSandbox
from test_resolve_acquisition_consumer import UPDATED, deployed_state


class AcquisitionConsumerWorkflowTest(WorkflowSandbox):
    script = "resolve-acquisition-consumer.py"
    initial_state = staticmethod(deployed_state)
    input_names = ("REQUESTED_ACQUISITION_DIGEST", "REQUESTED_ACQUISITION_STATE")
    apply_step = "Resolve Acquisition consumer image and state"
    plan_step = "Preserve Acquisition consumer image and state from state"
    settings_file = "acquisition-consumer.auto.tfvars.json"
    temporary_pattern = "acquisition-consumer-vars.*"

    def test_state_pull_failure_never_installs_partial_configuration(self):
        self.executable("terraform", "#!/bin/sh\ncat state.json\nexit 42\n")
        for run in (self.plan, self.apply):
            with self.subTest(step=run.__name__):
                self.assert_no_settings(run())

    def test_plan_and_apply_keep_existing_enabled_state(self):
        for run in (self.plan, self.apply):
            with self.subTest(step=run.__name__):
                result = run()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(self.settings()["acquisition_consumer_enabled"])

    def test_explicit_stop_reaches_resolver(self):
        self.env["REQUESTED_ACQUISITION_STATE"] = "disabled"
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.settings()["acquisition_consumer_enabled"])

    def test_missing_image_does_not_install_configuration(self):
        self.ecr_stubs()
        self.env["REQUESTED_ACQUISITION_DIGEST"] = UPDATED
        self.executable("aws", "#!/bin/sh\nprintf '%s\\n' None\n")
        self.assert_no_settings(self.apply())

    def test_verified_image_is_installed(self):
        self.ecr_stubs()
        self.env["REQUESTED_ACQUISITION_DIGEST"] = UPDATED
        self.executable("aws", f"#!/bin/sh\nprintf '%s\\n' '{UPDATED}'\n")
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.settings()["acquisition_consumer_image_digest"], UPDATED)
