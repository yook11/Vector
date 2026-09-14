"""補完のデプロイで既存の版と受信状態を保持する。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "infra/aws/scripts/resolve-completion-images.py"
pytestmark = pytest.mark.unit
DIGEST = "sha256:" + "a" * 64


def state(enabled=True):
    return {
        "resources": [
            {
                "mode": "managed",
                "type": "aws_lambda_function",
                "name": name,
                "instances": [{"attributes": {"image_uri": "backend@" + DIGEST}}],
            }
            for name in ("completion_consumer", "completion_outbox_relay")
        ]
        + [
            {
                "mode": "managed",
                "type": "aws_lambda_event_source_mapping",
                "name": "completion_consumer",
                "instances": [{"attributes": {"enabled": enabled}}],
            },
            {
                "mode": "managed",
                "type": "aws_scheduler_schedule",
                "name": "completion_outbox_relay",
                "instances": [
                    {"attributes": {"state": "ENABLED" if enabled else "DISABLED"}}
                ],
            },
        ]
    }


def resolve(data, *arguments):
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), *arguments],
        input=json.dumps(data),
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize("enabled", [True, False])
def test_normal_deployment_preserves_images_and_activation(enabled):
    """別件のapplyで補完の版や稼働状態を変更しない。"""
    result = resolve(state(enabled))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "completion_consumer_image_digest": DIGEST,
        "completion_outbox_relay_image_digest": DIGEST,
        "completion_consumer_enabled": enabled,
        "completion_relay_enabled": enabled,
    }


def test_initial_images_do_not_start_processing():
    """初回の版を指定しただけでは受信と定期送信を開始しない。"""
    result = resolve(
        {"resources": []}, "--consumer-digest", DIGEST, "--relay-digest", DIGEST
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "completion_consumer_image_digest": DIGEST,
        "completion_outbox_relay_image_digest": DIGEST,
        "completion_consumer_enabled": False,
        "completion_relay_enabled": False,
    }


def test_explicit_activation_changes_only_requested_side():
    """受信を有効化してもRelayは明示するまで停止を維持する。"""
    result = resolve(state(False), "--consumer-state", "enabled")
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    assert config["completion_consumer_enabled"] is True
    assert config["completion_relay_enabled"] is False


def test_activation_without_image_is_rejected():
    """存在しないLambdaの起動を設定成功として扱わない。"""
    result = resolve({"resources": []}, "--consumer-state", "enabled")
    assert result.returncode != 0
    assert result.stdout == ""


def test_invalid_saved_activation_does_not_emit_configuration():
    """不正な保存状態を暗黙に停止へ変換しない。"""
    data = state()
    data["resources"][2]["instances"][0]["attributes"]["enabled"] = "private-invalid"
    result = resolve(data)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "private-invalid" not in result.stderr


@pytest.mark.parametrize(
    "workflow", ["aws-terraform-plan.yml", "aws-terraform-apply.yml"]
)
@pytest.mark.parametrize("state_fails", [False, True])
def test_ci_preserves_both_images_or_stops_before_writing_settings(
    tmp_path, workflow, state_fails
):
    """実CIのshellが両版を保持し、state取得失敗時は設定を配置せず停止する。"""
    stage = "completion"
    doc = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    job = doc["jobs"]["plan" if "plan" in workflow else "apply"]
    step = next(
        s for s in job["steps"] if f"{stage.title()} images" in s.get("name", "")
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = SCRIPT.with_name(f"resolve-{stage}-images.py")
    (scripts / script.name).write_text(script.read_text())
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "python3").symlink_to(sys.executable)
    terraform = binaries / "terraform"
    terraform.write_text(
        "#!/bin/sh\nexit 42\n"
        if state_fails
        else "#!/bin/sh\ncat <<'JSON'\n"
        + json.dumps(state()).replace("assessment", stage)
        + "\nJSON\n"
    )
    terraform.chmod(0o755)
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", step["run"]],
        cwd=tmp_path,
        env={
            "PATH": f"{binaries}:{os.defpath}",
            "REQUESTED_CONSUMER_DIGEST": "",
            "REQUESTED_RELAY_DIGEST": "",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    settings = tmp_path / f"{stage}.auto.tfvars.json"
    if state_fails:
        assert result.returncode != 0
        assert not settings.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert json.loads(settings.read_text()) == {
            f"{stage}_consumer_image_digest": DIGEST,
            f"{stage}_outbox_relay_image_digest": DIGEST,
            "completion_consumer_enabled": True,
            "completion_relay_enabled": True,
        }
    assert not list(tmp_path.glob(f"{stage}-vars.*"))


@pytest.mark.parametrize("relay_image_exists", [True, False])
def test_apply_requires_both_requested_images_in_backend_ecr(
    tmp_path, relay_image_exists
):
    """Consumerのイメージが存在しても、指定したrelayイメージがなければ適用前に停止する。"""
    stage = "completion"
    doc = yaml.safe_load(
        (ROOT / ".github/workflows/aws-terraform-apply.yml").read_text()
    )
    step = next(
        s
        for s in doc["jobs"]["apply"]["steps"]
        if s.get("name") == f"Resolve {stage.title()} images"
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = SCRIPT.with_name(f"resolve-{stage}-images.py")
    (scripts / script.name).write_text(script.read_text())
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "python3").symlink_to(sys.executable)
    (tmp_path / "state.json").write_text(
        json.dumps(state()).replace("assessment", stage)
    )
    terraform = binaries / "terraform"
    terraform.write_text(
        '#!/bin/sh\nif [ "$1" = state ]; then\ncat state.json\nelse\n'
        "printf '%s\\n' '{\"backend\":\"registry.invalid/test/backend\"}'\nfi\n"
    )
    terraform.chmod(0o755)
    aws = binaries / "aws"
    aws.write_text(
        '#!/bin/sh\nfor arg in "$@"; do\n'
        'case "$arg" in imageDigest=*) digest=${arg#imageDigest=} ;; esac\ndone\n'
        + (
            f'if [ "$digest" = "{DIGEST}" ]; then digest=None; fi\n'
            if not relay_image_exists
            else ""
        )
        + "printf '%s\\n' \"$digest\"\n"
    )
    aws.chmod(0o755)
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", step["run"]],
        cwd=tmp_path,
        env={
            "PATH": f"{binaries}:/opt/homebrew/bin:{os.defpath}",
            "REQUESTED_CONSUMER_DIGEST": ("sha256:" + "b" * 64),
            "REQUESTED_RELAY_DIGEST": DIGEST,
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (result.returncode == 0) is relay_image_exists, result.stderr
    if relay_image_exists:
        assert json.loads((tmp_path / f"{stage}.auto.tfvars.json").read_text()) == {
            f"{stage}_consumer_image_digest": ("sha256:" + "b" * 64),
            f"{stage}_outbox_relay_image_digest": DIGEST,
            "completion_consumer_enabled": True,
            "completion_relay_enabled": True,
        }
    else:
        assert not (tmp_path / "completion.auto.tfvars.json").exists()
    assert not list(tmp_path.glob("completion-vars.*"))
