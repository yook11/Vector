"""既存と同じ配備shell検証で、投入設定を安全に配置することを確認する。"""

from test_backfill_workflows import WorkflowSandbox
from test_resolve_source_dispatch import UPDATED, deployed_state


class SourceDispatchWorkflowTest(WorkflowSandbox):
    script = "resolve-source-dispatch.py"
    initial_state = staticmethod(deployed_state)
    input_names = ("REQUESTED_DISPATCH_DIGEST", "REQUESTED_DISPATCH_STATE")
    apply_step = "Resolve Source dispatch image and schedules"
    plan_step = "Preserve Source dispatch image and schedules from state"
    settings_file = "source-dispatch.auto.tfvars.json"
    temporary_pattern = "source-dispatch-vars.*"

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
                self.assertTrue(self.settings()["source_dispatch_enabled"])

    def test_explicit_stop_reaches_resolver(self):
        self.env["REQUESTED_DISPATCH_STATE"] = "disabled"
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.settings()["source_dispatch_enabled"])

    def test_missing_image_does_not_install_configuration(self):
        self.ecr_stubs()
        self.env["REQUESTED_DISPATCH_DIGEST"] = UPDATED
        self.executable("aws", "#!/bin/sh\nprintf '%s\\n' None\n")
        self.assert_no_settings(self.apply())

    def test_verified_image_is_installed(self):
        self.ecr_stubs()
        self.env["REQUESTED_DISPATCH_DIGEST"] = UPDATED
        self.executable("aws", f"#!/bin/sh\nprintf '%s\\n' '{UPDATED}'\n")
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.settings()["source_dispatch_image_digest"], UPDATED)
