"""Assessmentの2つのイメージを独立して保持し、CIが解決失敗時に停止することを確認する。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "infra/aws/scripts/resolve-assessment-images.py"
CONSUMER = "sha256:" + "a" * 64
RELAY = "sha256:" + "b" * 64
NEXT = "sha256:" + "c" * 64
pytestmark = pytest.mark.unit


def deployed_state():
    return {
        "resources": [
            {
                "mode": "managed",
                "type": "aws_lambda_function",
                "name": name,
                "instances": [{"attributes": {"image_uri": f"backend@{digest}"}}],
            }
            for name, digest in [
                ("assessment_consumer", CONSUMER),
                ("assessment_outbox_relay", RELAY),
                ("embedding_consumer", NEXT),
            ]
        ]
    }


@pytest.mark.parametrize(
    ("state", "consumer", "relay", "expected_consumer", "expected_relay"),
    [
        ({"resources": []}, "", "", None, None),
        (deployed_state(), "", "", CONSUMER, RELAY),
        (deployed_state(), NEXT, "", NEXT, RELAY),
        (deployed_state(), "", NEXT, CONSUMER, NEXT),
    ],
)
def test_resolves_each_assessment_image_independently(
    state, consumer, relay, expected_consumer, expected_relay
):
    """初回は未起動とし、通常は両版を保持して明示した側だけを更新する。"""
    result = subprocess.run(  # noqa: S603
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
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "assessment_consumer_image_digest": expected_consumer,
        "assessment_outbox_relay_image_digest": expected_relay,
    }


def test_invalid_relay_state_does_not_emit_partial_configuration():
    """Consumer側を解決できてもrelayのstateが不正なら設定を一切出力しない。"""
    state = deployed_state()
    state["resources"][1]["instances"][0]["attributes"]["image_uri"] = "backend:latest"
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT)],
        input=json.dumps(state),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert result.stdout == ""


@pytest.mark.parametrize(
    "workflow", ["aws-terraform-plan.yml", "aws-terraform-apply.yml"]
)
@pytest.mark.parametrize("state_fails", [False, True])
def test_ci_preserves_both_images_or_stops_before_writing_settings(
    tmp_path, workflow, state_fails
):
    """実CIのshellが両版を保持し、state取得失敗時は設定を配置せず停止する。"""
    doc = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    job = doc["jobs"]["plan" if "plan" in workflow else "apply"]
    step = next(s for s in job["steps"] if "Assessment images" in s.get("name", ""))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / SCRIPT.name).write_text(SCRIPT.read_text())
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "python3").symlink_to(sys.executable)
    terraform = binaries / "terraform"
    terraform.write_text(
        "#!/bin/sh\nexit 42\n"
        if state_fails
        else "#!/bin/sh\ncat <<'JSON'\n" + json.dumps(deployed_state()) + "\nJSON\n"
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
    settings = tmp_path / "assessment.auto.tfvars.json"
    if state_fails:
        assert result.returncode != 0
        assert not settings.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert json.loads(settings.read_text()) == {
            "assessment_consumer_image_digest": CONSUMER,
            "assessment_outbox_relay_image_digest": RELAY,
        }
    assert not list(tmp_path.glob("assessment-vars.*"))


@pytest.mark.parametrize("relay_image_exists", [True, False])
def test_apply_requires_both_requested_images_in_backend_ecr(
    tmp_path, relay_image_exists
):
    """Consumerのイメージが存在しても、指定したrelayイメージがなければ適用前に停止する。"""
    doc = yaml.safe_load(
        (ROOT / ".github/workflows/aws-terraform-apply.yml").read_text()
    )
    step = next(
        s
        for s in doc["jobs"]["apply"]["steps"]
        if s.get("name") == "Resolve Assessment images"
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / SCRIPT.name).write_text(SCRIPT.read_text())
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "python3").symlink_to(sys.executable)
    (tmp_path / "state.json").write_text(json.dumps(deployed_state()))
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
            f'if [ "$digest" = "{RELAY}" ]; then digest=None; fi\n'
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
            "REQUESTED_CONSUMER_DIGEST": NEXT,
            "REQUESTED_RELAY_DIGEST": RELAY,
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (result.returncode == 0) is relay_image_exists, result.stderr
    if relay_image_exists:
        assert json.loads((tmp_path / "assessment.auto.tfvars.json").read_text()) == {
            "assessment_consumer_image_digest": NEXT,
            "assessment_outbox_relay_image_digest": RELAY,
        }
