"""Consumerの版指定とCIの失敗時停止を、実際のスクリプトで検証する。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "infra/aws/scripts/resolve-embedding-consumer-image.py"
CURRENT = "sha256:" + "a" * 64
NEXT = "sha256:" + "b" * 64
pytestmark = pytest.mark.unit


def consumer_state(image=f"example.invalid/backend@{CURRENT}"):
    return {
        "resources": [
            {
                "mode": "managed",
                "type": "aws_lambda_function",
                "name": "embedding_consumer",
                "instances": [{"attributes": {"image_uri": image}}],
            }
        ],
    }


def resolve(state, requested=""):
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), "--digest", requested],
        input=json.dumps(state),
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize(
    ("state", "requested", "expected"),
    [
        ({"resources": []}, "", None),
        ({"resources": []}, NEXT, NEXT),
        (consumer_state(), "", CURRENT),
        (consumer_state(), NEXT, NEXT),
        (consumer_state(f"example.invalid/backend@{NEXT}"), CURRENT, CURRENT),
    ],
)
def test_digest_creation_preservation_update_and_rollback(state, requested, expected):
    result = resolve(state, requested)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"embedding_consumer_image_digest": expected}


@pytest.mark.parametrize(
    "state",
    [
        None,
        {},
        {"resources": None},
        consumer_state("backend:latest"),
        consumer_state("backend@sha256:bad"),
    ],
)
def test_invalid_state_never_emits_null(state):
    result = resolve(state, NEXT)
    assert result.returncode != 0
    assert result.stdout == ""


@pytest.mark.parametrize(
    "requested", ["latest", "sha256:" + "A" * 64, "sha256:" + "a" * 63, " " + CURRENT]
)
def test_invalid_requested_digest_is_rejected(requested):
    result = resolve(consumer_state(), requested)
    assert result.returncode != 0 and result.stdout == ""


def test_relay_and_deposed_images_do_not_replace_consumer():
    state = consumer_state()
    state["resources"][0]["instances"].append(
        {"deposed": "old", "attributes": {"image_uri": "old:tag"}}
    )
    relay = consumer_state(f"backend@{NEXT}")["resources"][0]
    relay["name"] = "outbox_relay"
    state["resources"].append(relay)
    assert (
        json.loads(resolve(state).stdout)["embedding_consumer_image_digest"] == CURRENT
    )


def test_multiple_current_images_are_rejected():
    state = consumer_state()
    state["resources"][0]["instances"] *= 2
    result = resolve(state)
    assert result.returncode != 0 and result.stdout == ""


def workflow_step(workflow):
    doc = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    job = doc["jobs"]["plan" if "plan" in workflow else "apply"]
    return job, next(
        s for s in job["steps"] if "Embedding Consumer image" in s.get("name", "")
    )


@pytest.mark.parametrize(
    "workflow", ["aws-terraform-plan.yml", "aws-terraform-apply.yml"]
)
@pytest.mark.parametrize("state_fails", [False, True])
def test_workflows_preserve_digest_and_stop_on_state_failure(
    tmp_path, workflow, state_fails
):
    job, step = workflow_step(workflow)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / SCRIPT.name).write_text(SCRIPT.read_text())
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "python3").symlink_to(sys.executable)
    terraform = binaries / "terraform"
    terraform.write_text(
        f"#!/bin/sh\nexit {42 if state_fails else 0}\n"
        if state_fails
        else "#!/bin/sh\ncat <<'JSON'\n" + json.dumps(consumer_state()) + "\nJSON\n"
    )
    terraform.chmod(0o755)
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", step["run"]],
        cwd=tmp_path,
        env={"PATH": f"{binaries}:{os.defpath}", "REQUESTED_DIGEST": ""},
        capture_output=True,
        text=True,
        timeout=10,
    )
    output = tmp_path / "embedding-consumer.auto.tfvars.json"
    if state_fails:
        assert result.returncode != 0 and not output.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert (
            json.loads(output.read_text())["embedding_consumer_image_digest"] == CURRENT
        )
    assert not list(tmp_path.glob("embedding-consumer-vars.*"))
    if "apply" in workflow:
        assert job["environment"] == "production"
        assert job["concurrency"]["cancel-in-progress"] is False
        assert "steps.image.outputs.tag" in str(job)


def test_apply_checks_requested_image_before_plan():
    job, step = workflow_step("aws-terraform-apply.yml")
    assert (
        step["env"]["REQUESTED_DIGEST"]
        == "${{ inputs.embedding_consumer_image_digest }}"
    )
    assert 'if [ -n "$REQUESTED_DIGEST" ]' in step["run"]
    assert 'aws ecr describe-images --repository-name "$repository"' in step["run"]
    assert "imageDigest=$REQUESTED_DIGEST" in step["run"]
    assert 'if [ "$image" != "$REQUESTED_DIGEST" ]' in step["run"]
    assert "exit 1" in step["run"]
    assert job["steps"].index(step) < next(
        i
        for i, s in enumerate(job["steps"])
        if s.get("name") == "Terraform plan and apply"
    )


@pytest.mark.parametrize("ecr_result", ["present", "missing", "api_failure"])
def test_apply_resolution_requires_requested_image_in_ecr(tmp_path, ecr_result):
    _, step = workflow_step("aws-terraform-apply.yml")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / SCRIPT.name).write_text(SCRIPT.read_text())
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "python3").symlink_to(sys.executable)
    terraform = binaries / "terraform"
    repositories = json.dumps({"backend": "registry.invalid/test/backend"})
    terraform.write_text(
        "#!/bin/sh\nif [ \"$1\" = state ]; then\ncat <<'JSON'\n"
        + json.dumps(consumer_state())
        + "\nJSON\nelse\nprintf '%s\\n' '"
        + repositories
        + "'\nfi\n"
    )
    terraform.chmod(0o755)
    aws = binaries / "aws"
    reported_digest = NEXT if ecr_result == "present" else "None"
    aws.write_text(
        "#!/bin/sh\nexit 43\n"
        if ecr_result == "api_failure"
        else f"#!/bin/sh\nprintf '%s\\n' '{reported_digest}'\n"
    )
    aws.chmod(0o755)
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", step["run"]],
        cwd=tmp_path,
        env={
            "PATH": f"{binaries}:/opt/homebrew/bin:{os.defpath}",
            "REQUESTED_DIGEST": NEXT,
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (result.returncode == 0) == (ecr_result == "present"), result.stderr
    if ecr_result == "present":
        assert (
            json.loads((tmp_path / "embedding-consumer.auto.tfvars.json").read_text())[
                "embedding_consumer_image_digest"
            ]
            == NEXT
        )
    else:
        assert not (tmp_path / "summary").exists()
