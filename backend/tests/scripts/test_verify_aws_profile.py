"""ローカルAWS caller preflightの契約テスト。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "infra" / "aws" / "scripts" / "verify-aws-profile.sh"
_ACCOUNT_ID = "111122223333"
_PROFILES = "\n".join(
    (
        "default",
        "vector-deploy",
        "vector-plan",
        "vector-apply",
        "vector-push",
        "vector-rollout",
        "vector-migrate",
        "vector-admin",
    )
)
_CREDENTIAL_ENV_NAMES = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
)


def _install_fake_aws(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws = bin_dir / "aws"
    aws.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf 'called\\n' >> "$FAKE_AWS_CALLS"
if [ "${1-}" = configure ] && [ "${2-}" = list-profiles ]; then
  printf '%s\\n' "$FAKE_AWS_PROFILES"
  exit 0
fi
if [ "${1-}" = configure ] && [ "${2-}" = get ]; then
  if [ "${3-}" = sso_account_id ]; then
    printf '%s\\n' "$FAKE_AWS_CONFIG_ACCOUNT"
  elif [ "${3-}" = role_arn ]; then
    printf 'arn:aws:iam::%s:role/vector-ci/fake-role\\n' "$FAKE_AWS_CONFIG_ACCOUNT"
  else
    exit 2
  fi
  exit 0
fi
if [ "${1-}" = sts ] && [ "${2-}" = get-caller-identity ]; then
  if [ "${FAKE_AWS_STATUS:-0}" -ne 0 ]; then
    echo "${FAKE_AWS_ERROR:-No access}" >&2
    exit "$FAKE_AWS_STATUS"
  fi
  printf '%s\\t%s\\n' "$FAKE_AWS_CALLER_ACCOUNT" "$FAKE_AWS_ARN"
  exit 0
fi
exit 2
""",
        encoding="utf-8",
    )
    aws.chmod(0o755)
    return bin_dir


def _run(
    tmp_path: Path,
    *args: str,
    arn: str = "",
    extra_env: dict[str, str] | None = None,
    aws_status: int = 0,
    aws_error: str = "",
    config_account: str = _ACCOUNT_ID,
    caller_account: str = _ACCOUNT_ID,
) -> subprocess.CompletedProcess[str]:
    bin_dir = _install_fake_aws(tmp_path)
    env = os.environ.copy()
    for name in (*_CREDENTIAL_ENV_NAMES, "AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        env.pop(name, None)
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "FAKE_AWS_ARN": arn,
            "FAKE_AWS_PROFILES": _PROFILES,
            "FAKE_AWS_STATUS": str(aws_status),
            "FAKE_AWS_CALLS": str(tmp_path / "aws-calls"),
            "FAKE_AWS_ERROR": aws_error,
            "FAKE_AWS_CONFIG_ACCOUNT": config_account,
            "FAKE_AWS_CALLER_ACCOUNT": caller_account,
        }
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(  # noqa: S603
        [_SCRIPT, *args],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )


@pytest.mark.parametrize(
    ("profile", "role", "display"),
    (
        (
            "default",
            "AWSReservedSSO_ReadOnly_abc123",
            "AWSReservedSSO_ReadOnly_<SUFFIX>",
        ),
        (
            "vector-deploy",
            "AWSReservedSSO_VectorDeploy_abc123",
            "AWSReservedSSO_VectorDeploy_<SUFFIX>",
        ),
        ("vector-plan", "vector-ci-terraform-plan", "vector-ci-terraform-plan"),
        ("vector-push", "vector-ci-app-push", "vector-ci-app-push"),
        (
            "vector-admin",
            "AWSReservedSSO_WorkloadAdministrator_abc123",
            "AWSReservedSSO_WorkloadAdministrator_<SUFFIX>",
        ),
    ),
)
def test_accepts_only_the_expected_caller_role(
    tmp_path: Path, profile: str, role: str, display: str
) -> None:
    arn = f"arn:aws:sts::{_ACCOUNT_ID}:assumed-role/{role}/test-session"

    result = _run(tmp_path, profile, arn=arn)

    assert (result.returncode, result.stderr) == (0, "")
    assert result.stdout.strip() == f"AWS profile {profile}: caller role {display}"
    assert _ACCOUNT_ID not in result.stdout
    assert "arn:aws" not in result.stdout


def test_rejects_an_unexpected_role_without_echoing_the_arn(tmp_path: Path) -> None:
    arn = f"arn:aws:sts::{_ACCOUNT_ID}:assumed-role/AdministratorAccess/test-session"

    result = _run(tmp_path, "vector-plan", arn=arn)

    assert result.returncode == 1
    assert "caller roleが期待値と一致しません" in result.stderr
    assert "aws sso login --profile vector-deploy" in result.stderr
    assert _ACCOUNT_ID not in result.stderr
    assert arn not in result.stderr


def test_rejects_the_expected_role_from_a_different_account(tmp_path: Path) -> None:
    role = "vector-ci-terraform-plan"
    arn = f"arn:aws:sts::{_ACCOUNT_ID}:assumed-role/{role}/test-session"

    result = _run(
        tmp_path,
        "vector-plan",
        arn=arn,
        config_account="999900001111",
    )

    assert result.returncode == 1
    assert "caller accountが設定と一致しません" in result.stderr
    assert _ACCOUNT_ID not in result.stderr
    assert "999900001111" not in result.stderr
    assert arn not in result.stderr


@pytest.mark.parametrize(
    ("profile", "login_profile"),
    (("vector-plan", "vector-deploy"), ("vector-admin", "vector-admin")),
)
def test_auth_failure_reports_only_the_explicit_login_command(
    tmp_path: Path, profile: str, login_profile: str
) -> None:
    raw_error = f"No access for arn:aws:iam::{_ACCOUNT_ID}:role/private-role"

    result = _run(
        tmp_path,
        profile,
        aws_status=254,
        aws_error=raw_error,
    )

    assert result.returncode == 1
    assert f"aws sso login --profile {login_profile}" in result.stderr
    assert raw_error not in result.stderr
    assert _ACCOUNT_ID not in result.stderr


@pytest.mark.parametrize("name", _CREDENTIAL_ENV_NAMES)
def test_rejects_ambient_credential_environment_variables(
    tmp_path: Path, name: str
) -> None:
    result = _run(
        tmp_path,
        "vector-plan",
        arn="unused",
        extra_env={name: "sensitive-value"},
    )

    assert result.returncode == 1
    assert name in result.stderr
    assert "sensitive-value" not in result.stderr


@pytest.mark.parametrize("name", ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"))
def test_rejects_a_conflicting_profile_environment_variable(
    tmp_path: Path, name: str
) -> None:
    result = _run(
        tmp_path,
        "vector-plan",
        arn="unused",
        extra_env={name: "vector-admin"},
    )

    assert result.returncode == 1
    assert "検証対象profileと一致しません" in result.stderr
    assert "vector-admin" not in result.stderr


def test_allows_a_matching_profile_environment_variable(tmp_path: Path) -> None:
    role = "vector-ci-terraform-plan"
    arn = f"arn:aws:sts::{_ACCOUNT_ID}:assumed-role/{role}/test-session"

    result = _run(
        tmp_path,
        "vector-plan",
        arn=arn,
        extra_env={"AWS_PROFILE": "vector-plan"},
    )

    assert result.returncode == 0


@pytest.mark.parametrize("args", ((), ("unknown-profile",)))
def test_usage_errors_return_ex_usage(tmp_path: Path, args: tuple[str, ...]) -> None:
    result = _run(tmp_path, *args, arn="unused")

    assert result.returncode == 64
    assert "usage:" in result.stderr


@pytest.mark.parametrize(
    "profile", ["vector-apply", "vector-migrate", "vector-rollout"]
)
def test_retired_profiles_are_rejected_before_any_aws_command(
    tmp_path: Path,
    profile: str,
) -> None:
    result = _run(tmp_path, profile, arn="must-not-be-used")
    assert (result.returncode, result.stdout) == (64, "")
    assert not (tmp_path / "aws-calls").exists()
    assert "専用GitHub Actions" in result.stderr
