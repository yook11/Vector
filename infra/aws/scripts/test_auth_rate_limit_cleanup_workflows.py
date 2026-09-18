"""実workflowで掃除Lambdaの停止状態とイメージ検証を維持する。"""

from test_backfill_workflows import WorkflowSandbox
from test_resolve_auth_rate_limit_cleanup import UPDATED, deployed_state


class AuthRateLimitCleanupWorkflowTest(WorkflowSandbox):
    script = "resolve-auth-rate-limit-cleanup.py"
    initial_state = staticmethod(deployed_state)
    input_names = ("REQUESTED_DIGEST", "REQUESTED_STATE")
    apply_step = "Resolve auth rate limit cleanup image and schedule"
    plan_step = "Preserve auth rate limit cleanup image and schedule from state"
    settings_file = "auth-rate-limit-cleanup.auto.tfvars.json"
    temporary_pattern = "auth-rate-limit-cleanup-vars.*"

    def test_state_pull_failure_never_installs_partial_configuration(self):
        self.executable("terraform", "#!/bin/sh\ncat state.json\nexit 42\n")
        for run in (self.plan, self.apply):
            with self.subTest(step=run.__name__):
                self.assert_no_settings(run())

    def test_plan_and_apply_keep_existing_disabled_state(self):
        for run in (self.plan, self.apply):
            with self.subTest(step=run.__name__):
                result = run()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(self.settings()["auth_rate_limit_cleanup_enabled"])

    def test_explicit_enable_reaches_resolver(self):
        self.env["REQUESTED_STATE"] = "enabled"
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.settings()["auth_rate_limit_cleanup_enabled"])

    def test_missing_image_does_not_install_configuration(self):
        self.ecr_stubs()
        self.env["REQUESTED_DIGEST"] = UPDATED
        self.executable("aws", "#!/bin/sh\nprintf '%s\\n' None\n")
        self.assert_no_settings(self.apply())

    def test_verified_image_is_installed(self):
        self.ecr_stubs()
        self.env["REQUESTED_DIGEST"] = UPDATED
        self.executable("aws", f"#!/bin/sh\nprintf '%s\\n' '{UPDATED}'\n")
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.settings()["auth_rate_limit_cleanup_image_digest"], UPDATED
        )
