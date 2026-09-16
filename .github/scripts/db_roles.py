"""承認対象のロール定義とimageを固定し、専用taskだけを実行する。"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from check_app_release import check_app_release
from migration_ledger import GitHubCli
from migration_prepare import GitReleaseRepository

from scripts.db_role_runner import load_manifest

_FILES = (
    "scripts/db_role_runner.py",
    "db_roles.json",
    "app/db/rds-ca-ap-northeast-1.pem",
)


class RoleControlError(ValueError):
    """公開可能な固定理由だけを保持する。"""


def command(*args: str) -> bytes:
    executable = shutil.which(args[0])
    if executable is None:
        raise RoleControlError("command_unavailable")
    result = subprocess.run(  # noqa: S603
        [executable, *args[1:]], capture_output=True, timeout=120, check=False
    )
    if result.returncode:
        raise RoleControlError("command_failed")
    return result.stdout


class Aws:
    def call(self, *args: str) -> dict:
        return json.loads(command("aws", *args, "--output", "json"))


def read_manifest(root: Path) -> tuple[str, tuple[str, ...]]:
    path = root / "backend/db_roles.json"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest, load_manifest(path, digest)


def check_source(args) -> None:
    check_app_release(
        GitReleaseRepository(args.repo_root),
        GitHubCli(args.repo),
        release_sha=args.release_sha,
        check_schema=False,
    )


def verify_image(root: Path, image: str) -> None:
    inspection = json.loads(command("docker", "image", "inspect", image))[0]
    config = inspection["Config"]
    if (
        inspection.get("Architecture") != "arm64"
        or inspection.get("Os") != "linux"
        or config.get("Entrypoint") != ["python", "-m", "scripts.db_role_runner"]
        or config.get("Cmd") not in (None, [])
        or config.get("User") != "backend"
    ):
        raise RoleControlError("image_contract_mismatch")
    container = command("docker", "create", "--network", "none", image).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", container):
        raise RoleControlError("invalid_container")
    try:
        for path in _FILES:
            archive = command("docker", "cp", f"{container}:/app/{path}", "-")
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as stream:
                members = stream.getmembers()
                if len(members) != 1 or not members[0].isfile():
                    raise RoleControlError("image_file_invalid")
                reader = stream.extractfile(members[0])
                if (
                    reader is None
                    or reader.read() != (root / "backend" / path).read_bytes()
                ):
                    raise RoleControlError("image_file_mismatch")
    finally:
        command("docker", "rm", "--volumes", container)


def evidence(args, aws: Aws) -> dict:
    digest, roles = read_manifest(args.repo_root)
    account = aws.call("sts", "get-caller-identity")["Account"]
    if not re.fullmatch(r"\d{12}", account):
        raise RoleControlError("invalid_account")
    response = aws.call(
        "ecr",
        "batch-get-image",
        "--repository-name",
        f"{args.prefix}/backend",
        "--image-ids",
        f"imageTag=db-roles-{args.release_sha}",
    )
    images = response["images"]
    if len(images) != 1 or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", images[0]["imageId"]["imageDigest"]
    ):
        raise RoleControlError("invalid_digest")
    image = (
        f"{account}.dkr.ecr.{args.region}.amazonaws.com/{args.prefix}/backend"
        f"@{images[0]['imageId']['imageDigest']}"
    )
    command("docker", "pull", image)
    verify_image(args.repo_root, image)
    return {
        "release_sha": args.release_sha,
        "manifest_sha256": digest,
        "roles": list(roles),
        "image": image,
        "account": account,
        "run_id": args.run_id,
        "run_attempt": args.run_attempt,
    }


def read_evidence(args, aws: Aws) -> dict:
    value = json.loads(args.evidence.read_text())
    manifest_sha, roles = read_manifest(args.repo_root)
    account = aws.call("sts", "get-caller-identity")["Account"]
    expected = {
        "release_sha": args.release_sha,
        "manifest_sha256": manifest_sha,
        "roles": list(roles),
        "account": account,
        "run_id": args.run_id,
        "run_attempt": args.run_attempt,
    }
    image_prefix = (
        f"{account}.dkr.ecr.{args.region}.amazonaws.com/{args.prefix}/backend@"
    )
    if (
        set(value) != {*expected, "image"}
        or any(value[key] != item for key, item in expected.items())
        or not isinstance(value["image"], str)
        or not value["image"].startswith(image_prefix)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["image"][len(image_prefix) :])
    ):
        raise RoleControlError("evidence_mismatch")
    return value


def tags(args) -> dict[str, str]:
    return {
        "VectorPurpose": "db-roles",
        "ReleaseSha": args.release_sha,
        "GitHubRunId": args.run_id,
        "GitHubRunAttempt": args.run_attempt,
    }


def started_by(args) -> str:
    return f"roles-{args.run_id}-{args.run_attempt}"


def task_definition(args, record: dict, database: dict) -> dict:
    account = record["account"]
    base_role = (
        f"arn:aws:iam::{account}:role/{args.prefix}-db-admin/{args.prefix}-db-roles"
    )
    secret = database["MasterUserSecret"]["SecretArn"]
    if (
        database["DBInstanceIdentifier"] != f"{args.prefix}-db"
        or database["DBName"] != args.prefix
        or database["MasterUsername"] != f"{args.prefix}_master"
        or not secret.startswith(
            f"arn:aws:secretsmanager:{args.region}:{account}:secret:"
        )
        or database["MasterUserSecret"]["SecretStatus"] != "active"
    ):
        raise RoleControlError("database_contract_mismatch")
    environment = {
        "DB_ADMIN_HOST": database["Endpoint"]["Address"],
        "DB_ADMIN_PORT": str(database["Endpoint"]["Port"]),
        "DB_ADMIN_DATABASE": database["DBName"],
        "DB_ADMIN_USER": database["MasterUsername"],
        "DB_ROLES_RELEASE_SHA": args.release_sha,
        "DB_ROLES_MANIFEST_SHA256": record["manifest_sha256"],
    }
    return {
        "family": f"{args.prefix}-db-roles",
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": "256",
        "memory": "512",
        "runtimePlatform": {
            "cpuArchitecture": "ARM64",
            "operatingSystemFamily": "LINUX",
        },
        "taskRoleArn": f"{base_role}-task",
        "executionRoleArn": f"{base_role}-exec",
        "containerDefinitions": [
            {
                "name": "db-roles",
                "image": record["image"],
                "essential": True,
                "user": "1001",
                "privileged": False,
                "readonlyRootFilesystem": True,
                "entryPoint": ["python", "-m", "scripts.db_role_runner"],
                "command": [],
                "linuxParameters": {"capabilities": {"drop": ["ALL"]}},
                "environment": [
                    {"name": key, "value": value} for key, value in environment.items()
                ],
                "secrets": [
                    {"name": "DB_ADMIN_PASSWORD", "valueFrom": f"{secret}:password::"}
                ],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": f"/ecs/{args.prefix}-db-roles",
                        "awslogs-region": args.region,
                        "awslogs-stream-prefix": "ecs",
                    },
                },
            }
        ],
        "tags": [{"key": key, "value": value} for key, value in tags(args).items()],
    }


def require_owned(args, task: dict) -> None:
    actual = {item["key"]: item["value"] for item in task.get("tags", [])}
    family = task.get("taskDefinitionArn", "").rsplit("/", 1)[-1].split(":")[0]
    if (
        task.get("startedBy") != started_by(args)
        or family != f"{args.prefix}-db-roles"
        or any(actual.get(key) != value for key, value in tags(args).items())
        or task.get("enableExecuteCommand") is not False
    ):
        raise RoleControlError("task_ownership_mismatch")


def describe_owned(args, aws: Aws, arns: list[str]) -> list[dict]:
    if not arns:
        return []
    response = aws.call(
        "ecs",
        "describe-tasks",
        "--cluster",
        args.prefix,
        "--tasks",
        *arns,
        "--include",
        "TAGS",
    )
    if response.get("failures") or len(response["tasks"]) != len(arns):
        raise RoleControlError("task_unobservable")
    for task in response["tasks"]:
        require_owned(args, task)
    return response["tasks"]


def cleanup(args, aws: Aws) -> None:
    response = aws.call(
        "ecs", "list-tasks", "--cluster", args.prefix, "--started-by", started_by(args)
    )
    for task in describe_owned(args, aws, response["taskArns"]):
        if task["lastStatus"] != "STOPPED":
            aws.call(
                "ecs",
                "stop-task",
                "--cluster",
                args.prefix,
                "--task",
                task["taskArn"],
                "--reason",
                "DB role workflow cleanup",
            )


def run(args, aws: Aws) -> None:
    record = read_evidence(args, aws)
    check_source(args)
    active = aws.call(
        "ecs",
        "list-tasks",
        "--cluster",
        args.prefix,
        "--family",
        f"{args.prefix}-db-roles",
    )["taskArns"]
    if active:
        raise RoleControlError("role_task_already_active")
    response = aws.call(
        "rds", "describe-db-instances", "--db-instance-identifier", f"{args.prefix}-db"
    )
    if len(response["DBInstances"]) != 1:
        raise RoleControlError("database_unobservable")
    database = response["DBInstances"][0]
    definition = task_definition(args, record, database)
    vpc = database["DBSubnetGroup"]["VpcId"]
    subnets = aws.call(
        "ec2",
        "describe-subnets",
        "--filters",
        f"Name=tag:Name,Values={args.prefix}-migration",
        f"Name=vpc-id,Values={vpc}",
    )["Subnets"]
    groups = aws.call(
        "ec2",
        "describe-security-groups",
        "--filters",
        f"Name=group-name,Values={args.prefix}-db-roles",
        f"Name=vpc-id,Values={vpc}",
    )["SecurityGroups"]
    if (
        len(subnets) != 1
        or len(groups) != 1
        or subnets[0].get("MapPublicIpOnLaunch") is not False
    ):
        raise RoleControlError("network_contract_mismatch")
    registered = aws.call(
        "ecs", "register-task-definition", "--cli-input-json", json.dumps(definition)
    )
    task_arn = registered["taskDefinition"]["taskDefinitionArn"]
    if task_arn.rsplit("/", 1)[-1].split(":")[0] != f"{args.prefix}-db-roles":
        raise RoleControlError("task_definition_mismatch")
    check_source(args)
    request = {
        "cluster": args.prefix,
        "taskDefinition": task_arn,
        "launchType": "FARGATE",
        "platformVersion": "1.4.0",
        "count": 1,
        "enableExecuteCommand": False,
        "startedBy": started_by(args),
        "clientToken": started_by(args),
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": [subnets[0]["SubnetId"]],
                "securityGroups": [groups[0]["GroupId"]],
                "assignPublicIp": "DISABLED",
            }
        },
        "tags": definition["tags"],
    }
    response = aws.call("ecs", "run-task", "--cli-input-json", json.dumps(request))
    if response.get("failures") or len(response["tasks"]) != 1:
        raise RoleControlError("task_start_failed")
    arn = response["tasks"][0]["taskArn"]
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        task = describe_owned(args, aws, [arn])[0]
        if task["lastStatus"] == "STOPPED":
            containers = task.get("containers", [])
            if (
                task.get("stopCode") != "EssentialContainerExited"
                or len(containers) != 1
                or containers[0].get("name") != "db-roles"
                or containers[0].get("exitCode") != 0
            ):
                raise RoleControlError("role_task_failed")
            return
        time.sleep(5)
    raise RoleControlError("role_task_timeout")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("image", "run", "cleanup"))
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--summary-file", type=Path)
    args = parser.parse_args(argv)
    try:
        if (
            not re.fullmatch(r"[0-9a-f]{40}", args.release_sha)
            or not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", args.prefix)
            or not re.fullmatch(r"[a-z]{2}-[a-z]+-\d", args.region)
            or not re.fullmatch(r"[1-9]\d{0,19}", args.run_id)
            or not re.fullmatch(r"[1-9]\d{0,4}", args.run_attempt)
        ):
            raise RoleControlError("invalid_request")
        aws = Aws()
        if args.operation == "image":
            check_source(args)
            record = evidence(args, aws)
            args.evidence.write_text(json.dumps(record))
            if args.summary_file:
                args.summary_file.write_text(
                    "## DB role creation\n\n"
                    f"- Release: `{args.release_sha}`\n"
                    f"- Roles: {', '.join(record['roles'])}\n"
                    f"- Image digest: `{record['image'].split('@')[1]}`\n"
                    f"- Manifest SHA256: `{record['manifest_sha256']}`\n"
                )
        elif args.operation == "cleanup":
            cleanup(args, aws)
        else:
            run(args, aws)
        print(
            json.dumps(
                {
                    "result": "success",
                    "operation": args.operation,
                    "release_sha": args.release_sha,
                }
            )
        )
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "result": "failed",
                    "operation": args.operation,
                    "reason": str(exc)
                    if isinstance(exc, RoleControlError)
                    else "control_failed",
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
