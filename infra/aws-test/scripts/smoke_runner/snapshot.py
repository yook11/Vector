"""削除に使う定義・接続先・入力を実行開始時に固定する。"""

import configparser
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from uuid import uuid4

from .common import LOCAL, REGION, ROOT, save


def load(path):
    return json.loads(path.read_text())


def files(directory):
    return sorted(
        p
        for p in directory.rglob("*")
        if p.is_file() and ".terraform" not in p.parts and "__pycache__" not in p.parts
    )


def create(directory, run_id, runner_profile):
    account = load(LOCAL / "account.json")
    images = load(LOCAL / "images.tfvars.json")
    expected = account["expected_account_id"]
    manager = account["smoke_aws_profile"]
    for value, pattern in [
        (expected, r"[0-9]{12}"),
        (manager, r"[A-Za-z0-9][A-Za-z0-9_.-]*"),
        (runner_profile, r"[A-Za-z0-9][A-Za-z0-9_.-]*"),
        (images["source_revision"], r"[a-f0-9]{40}"),
        (images["backend_image_digest"], r"sha256:[a-f0-9]{64}"),
        (images["proxy_image_digest"], r"sha256:[a-f0-9]{64}"),
    ]:
        if not re.fullmatch(pattern, value):
            raise ValueError("invalid_local_configuration")
    workspace = directory / "workspace"
    for relative in [
        "infra/aws-test/smoke",
        "infra/aws-test/modules",
        "backend/aws_tests",
    ]:
        shutil.copytree(
            ROOT / relative,
            workspace / relative,
            ignore=shutil.ignore_patterns(
                ".terraform",
                "*.tfstate*",
                "__pycache__",
                ".pytest_cache",
                "*.tfplan",
                "*.tfvars*",
                "*.tfbackend*",
                "*.log",
                "crash.*",
            ),
        )
    for relative in [
        "infra/aws-test/scripts/wait-lambda-eni-deletion.py",
        "infra/aws/templates/squid.conf.tftpl",
        "backend/app/http/non_public_ranges.json",
    ]:
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    # 個人のconfigから必要なSSOメタデータだけを保存する。
    source = configparser.RawConfigParser()
    source.read(Path.home() / ".aws/config")
    config = configparser.RawConfigParser()
    for profile, role in [
        (manager, "VectorTestManager"),
        (runner_profile, "VectorTestRunner"),
    ]:
        section = source["profile " + profile]
        if section["sso_account_id"] != expected or section["sso_role_name"] != role:
            raise ValueError("sso_profile_mismatch")
        keys = ["sso_session", "sso_account_id", "sso_role_name"]
        config["profile " + profile] = {key: section[key] for key in keys}
        config["profile " + profile]["region"] = REGION
        session_name = section["sso_session"]
        sso = source["sso-session " + session_name]
        config["sso-session " + session_name] = {
            key: sso[key]
            for key in ["sso_start_url", "sso_region", "sso_registration_scopes"]
            if key in sso
        }
    role_arn = (
        f"arn:aws:iam::{expected}:role/vector-test/bootstrap/vector-test-terraform"
    )
    config["profile vector-smoke-construction"] = {
        "role_arn": role_arn,
        "source_profile": manager,
        "duration_seconds": "3600",
        "region": REGION,
    }
    with (directory / "aws.config").open("w") as output:
        config.write(output)
    save(directory / "account.json", account)
    save(
        directory / "inputs.tfvars.json",
        {
            **images,
            "expected_account_id": expected,
            "aws_profile": manager,
            "run_id": run_id,
            "gemini_parameter_path": "/vector-test/embedding-consumer/gemini-api-key",
        },
    )
    save(
        directory / "backend.json",
        {
            "bucket": f"vector-test-tfstate-{expected}",
            "key": f"smoke/{run_id}/terraform.tfstate",
            "profile": manager,
            "allowed_account_ids": [expected],
            "assume_role": {"role_arn": role_arn, "duration": "1h"},
        },
    )
    hashes = {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in files(workspace)
    }
    for name in ["aws.config", "account.json", "inputs.tfvars.json", "backend.json"]:
        hashes[name] = hashlib.sha256((directory / name).read_bytes()).hexdigest()
    save(
        directory / "manifest.json",
        {
            "run_id": run_id,
            "runner_profile": runner_profile,
            "files": hashes,
            "owner_id": str(uuid4()),
        },
    )


def verify(directory):
    manifest = load(directory / "manifest.json")
    if manifest["run_id"] != directory.name:
        raise ValueError("run_id_mismatch")
    for name, expected in manifest["files"].items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
            raise ValueError("saved_inputs_changed")
    expected_paths = set(manifest["files"])
    for path in files(directory / "workspace"):
        if path.suffix in {".tf", ".tftpl", ".json", ".py"}:
            if str(path.relative_to(directory)) not in expected_paths:
                raise ValueError("unexpected_saved_definition")
    return manifest


def environment(directory):
    # 環境の静的認証情報やTF_VAR/TF_CLI_ARGSによる接続先・操作の上書きを受け入れない。
    return {
        key: value
        for key, value in {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(Path.home()),
            "AWS_CONFIG_FILE": str(directory / "aws.config"),
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "AWS_REGION": REGION,
            "AWS_DEFAULT_REGION": REGION,
            "AWS_PAGER": "",
            "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "true",
            "TF_IN_AUTOMATION": "1",
            "TF_INPUT": "0",
            "TF_WORKSPACE": "default",
            "PYTHONPATH": str(directory / "workspace/backend"),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }.items()
    }
