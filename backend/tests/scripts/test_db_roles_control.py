"""承認済みDBロールtaskの起動と停止の境界を検証する。"""

import importlib
import io
import json
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".github/scripts"))
control = importlib.import_module("db_roles")
pytestmark = pytest.mark.unit


@pytest.fixture
def args(tmp_path):
    return SimpleNamespace(
        repo_root=ROOT,
        repo="yook11/Vector",
        release_sha="a" * 40,
        run_id="123",
        run_attempt="2",
        region="ap-northeast-1",
        prefix="vector",
        evidence=tmp_path / "evidence.json",
    )


@pytest.fixture
def record(args):
    digest, roles = control.read_manifest(ROOT)
    return {
        "release_sha": args.release_sha,
        "manifest_sha256": digest,
        "roles": list(roles),
        "account": "123456789012",
        "run_id": "123",
        "run_attempt": "2",
        "image": "123456789012.dkr.ecr.ap-northeast-1.amazonaws.com/"
        "vector/backend@sha256:" + "b" * 64,
    }


@pytest.fixture
def database():
    return {
        "DBInstanceIdentifier": "vector-db",
        "DBName": "vector",
        "MasterUsername": "vector_master",
        "MasterUserSecret": {
            "SecretArn": "arn:aws:secretsmanager:ap-northeast-1:"
            "123456789012:secret:rds!cluster-admin",
            "SecretStatus": "active",
        },
        "Endpoint": {"Address": "database.example.invalid", "Port": 5432},
        "DBSubnetGroup": {"VpcId": "vpc-private"},
    }


@pytest.fixture
def task(args):
    return {
        "taskArn": "arn:aws:ecs:ap-northeast-1:123456789012:task/vector/abc",
        "taskDefinitionArn": "arn:aws:ecs:ap-northeast-1:123456789012:"
        "task-definition/vector-db-roles:7",
        "startedBy": "roles-123-2",
        "enableExecuteCommand": False,
        "tags": [
            {"key": key, "value": value} for key, value in control.tags(args).items()
        ],
        "lastStatus": "STOPPED",
        "stopCode": "EssentialContainerExited",
        "containers": [{"name": "db-roles", "exitCode": 0}],
    }


@pytest.fixture
def ready_run(monkeypatch, args, record, database, task):
    monkeypatch.setattr(control, "read_evidence", Mock(return_value=record))
    monkeypatch.setattr(control, "check_source", Mock())
    return [
        {"taskArns": []},
        {"DBInstances": [database]},
        {"Subnets": [{"SubnetId": "subnet-private", "MapPublicIpOnLaunch": False}]},
        {"SecurityGroups": [{"GroupId": "sg-role-only"}]},
        {"taskDefinition": {"taskDefinitionArn": task["taskDefinitionArn"]}},
    ]


def test_definition_passes_secret_reference_and_fixed_runner_only(
    args, record, database
):
    """管理者passwordはsecret参照だけを渡し任意の処理を指定できない。"""
    definition = control.task_definition(args, record, database)
    (container,) = definition["containerDefinitions"]
    assert container["image"] == record["image"]
    assert container["entryPoint"] == ["python", "-m", "scripts.db_role_runner"]
    assert container["command"] == []
    assert container["secrets"] == [
        {
            "name": "DB_ADMIN_PASSWORD",
            "valueFrom": database["MasterUserSecret"]["SecretArn"] + ":password::",
        }
    ]
    assert {item["name"]: item["value"] for item in container["environment"]} == {
        "DB_ADMIN_HOST": "database.example.invalid",
        "DB_ADMIN_PORT": "5432",
        "DB_ADMIN_DATABASE": "vector",
        "DB_ADMIN_USER": "vector_master",
        "DB_ROLES_RELEASE_SHA": args.release_sha,
        "DB_ROLES_MANIFEST_SHA256": record["manifest_sha256"],
    }


@pytest.mark.parametrize(
    ("field", "unapproved"),
    [
        ("release_sha", "c" * 40),
        ("run_attempt", "1"),
        ("manifest_sha256", "d" * 64),
    ],
)
def test_evidence_rejects_unapproved_execution_identity(
    args, record, field, unapproved
):
    """別の承認対象を表すevidenceを受け入れない。"""
    record[field] = unapproved
    args.evidence.write_text(json.dumps(record))
    aws = Mock()
    aws.call.return_value = {"Account": "123456789012"}
    with pytest.raises(control.RoleControlError, match="evidence_mismatch"):
        control.read_evidence(args, aws)


def test_successful_task_has_no_exec_and_never_reads_secret(args, task, ready_run):
    """成功経路も秘密の取得をECSに任せ、execを無効にする。"""
    aws = Mock()
    aws.call.side_effect = [*ready_run, {"tasks": [task]}, {"tasks": [task]}]
    control.run(args, aws)
    calls = [call.args for call in aws.call.call_args_list]
    request = json.loads(
        next(call[3] for call in calls if call[:2] == ("ecs", "run-task"))
    )
    assert request["enableExecuteCommand"] is False
    assert (
        request["networkConfiguration"]["awsvpcConfiguration"]["assignPublicIp"]
        == "DISABLED"
    )
    assert all(call[0] != "secretsmanager" for call in calls)


def test_failed_container_is_not_reported_as_success(args, task, ready_run):
    """taskが停止してもrunner失敗を成功扱いしない。"""
    task["containers"][0]["exitCode"] = 1
    aws = Mock()
    aws.call.side_effect = [*ready_run, {"tasks": [task]}, {"tasks": [task]}]
    with pytest.raises(control.RoleControlError, match="role_task_failed"):
        control.run(args, aws)


def test_running_task_deadline_is_failure(monkeypatch, args, task, ready_run):
    """終了を観測できないtaskは期限で失敗する。"""
    aws = Mock()
    aws.call.side_effect = [*ready_run, {"tasks": [task]}]
    monkeypatch.setattr(control.time, "monotonic", Mock(side_effect=[0, 601]))
    with pytest.raises(control.RoleControlError, match="role_task_timeout"):
        control.run(args, aws)


@pytest.mark.parametrize(
    ("field", "foreign"),
    [
        ("startedBy", "roles-124-2"),
        ("startedBy", "roles-123-1"),
        (
            "taskDefinitionArn",
            "arn:aws:ecs:ap-northeast-1:123456789012:task-definition/vector-backend:7",
        ),
    ],
)
def test_cleanup_never_stops_foreign_task(args, task, field, foreign):
    """別run・attempt・familyのtaskを停止しない。"""
    task[field] = foreign
    task["lastStatus"] = "RUNNING"
    aws = Mock()
    aws.call.side_effect = [{"taskArns": [task["taskArn"]]}, {"tasks": [task]}]
    with pytest.raises(control.RoleControlError, match="task_ownership_mismatch"):
        control.cleanup(args, aws)
    assert all(
        call.args[:2] != ("ecs", "stop-task") for call in aws.call.call_args_list
    )


def test_cleanup_finds_started_task_after_lost_run_response(args, task, ready_run):
    """RunTaskの応答を失っても自分のattemptを検索して停止できる。"""
    task["lastStatus"] = "RUNNING"
    aws = Mock()
    aws.call.side_effect = [
        *ready_run,
        control.RoleControlError("command_failed"),
        {"taskArns": [task["taskArn"]]},
        {"tasks": [task]},
        {},
    ]
    with pytest.raises(control.RoleControlError, match="command_failed"):
        control.run(args, aws)
    control.cleanup(args, aws)
    calls = [call.args for call in aws.call.call_args_list]
    assert (
        "ecs",
        "list-tasks",
        "--cluster",
        "vector",
        "--started-by",
        "roles-123-2",
    ) in calls
    assert calls[-1][:2] == ("ecs", "stop-task")
    assert task["taskArn"] in calls[-1]


def test_main_advance_after_preparation_prevents_run_task(monkeypatch, args, ready_run):
    """準備中にmainが進んだ場合はtask登録後でも起動しない。"""
    monkeypatch.setattr(
        control,
        "check_source",
        Mock(side_effect=[None, control.RoleControlError("main_advanced")]),
    )
    aws = Mock()
    aws.call.side_effect = ready_run
    with pytest.raises(control.RoleControlError, match="main_advanced"):
        control.run(args, aws)
    assert all(call.args[:2] != ("ecs", "run-task") for call in aws.call.call_args_list)


def test_image_content_mismatch_removes_verification_container(monkeypatch, tmp_path):
    """承認sourceと異なるimageを拒否し検証containerを残さない。"""
    source = tmp_path / "backend/scripts/db_role_runner.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"approved runner")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as stream:
        member = tarfile.TarInfo("db_role_runner.py")
        member.size = len(b"modified runner")
        stream.addfile(member, io.BytesIO(b"modified runner"))
    command = Mock(
        side_effect=[
            json.dumps(
                [
                    {
                        "Architecture": "arm64",
                        "Os": "linux",
                        "Config": {
                            "Entrypoint": ["python", "-m", "scripts.db_role_runner"],
                            "Cmd": [],
                            "User": "backend",
                        },
                    }
                ]
            ).encode(),
            b"a" * 64,
            archive.getvalue(),
            b"",
        ]
    )
    monkeypatch.setattr(control, "command", command)
    with pytest.raises(control.RoleControlError, match="image_file_mismatch"):
        control.verify_image(tmp_path, "image@sha256:approved")
    assert command.call_args.args == ("docker", "rm", "--volumes", "a" * 64)


def test_workflow_separates_build_credentials_from_approved_apply():
    """DB権限のあるjobは承認と本番共通直列化の内側で実行する。"""
    source = (ROOT / ".github/workflows/aws-db-roles.yml").read_text()
    loader = yaml.BaseLoader(source)
    try:
        workflow = loader.get_single_data()
    finally:
        loader.dispose()
    assert workflow["on"]["workflow_dispatch"] in (None, "", {})
    build, apply = workflow["jobs"]["build"], workflow["jobs"]["apply"]
    assert apply["needs"] == "build"
    assert apply["environment"] == "production-db-roles"
    assert apply["concurrency"]["group"] == "vector-production-change"
    assert apply["concurrency"]["cancel-in-progress"] == "false"
    assert "AWS_DB_ROLES_ROLE_ARN" not in json.dumps(build)
    assert "AWS_PUSH_ROLE_ARN" in json.dumps(build)
    assert "get-secret-value" not in source
    assert "AWS_DB_ROLES_ROLE_ARN" in json.dumps(apply)
