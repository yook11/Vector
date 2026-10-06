"""隔離アカウントで固定Automationの成功・拒否・撤去を測定する。"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
import uuid

import botocore.session
from botocore.config import Config
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "aws/scripts"))
from sqs_operations.aws_cli import AwsCli  # noqa: E402
from sqs_operations.bastion import (
    Configuration,
    status,
    verify_identity,
    start as cli_start,
)  # noqa: E402
from sqs_operations.redrive import QueuePair, operate  # noqa: E402
from sqs_operations.tunnel import sqs_tunnel  # noqa: E402

ACCOUNT = "733360597472"
REGION = "ap-northeast-1"
LOCAL = Path(__file__).parent / ".local"
OPTIONS = Config(
    connect_timeout=10,
    read_timeout=30,
    retries={"total_max_attempts": 1},
    ignore_configured_endpoint_urls=True,
)


def record(name, **result):
    value = {"at": datetime.now(timezone.utc).isoformat(), "check": name, **result}
    with (LOCAL / "results.jsonl").open("a") as stream:
        stream.write(json.dumps(value) + "\n")
    print(json.dumps(value), flush=True)


class Probe:
    def __init__(self):
        self.fixture = json.loads((LOCAL / "fixture.json").read_text())
        self.config = self.fixture["configuration"]
        self.session = botocore.session.get_session()
        self.session.set_config_variable("profile", "vector-test-admin")
        identity = self.admin("sts").get_caller_identity()
        prefix = "vector-test-auto-" + self.fixture["run_id"]
        if (
            identity["Account"] != ACCOUNT
            or self.fixture["account_id"] != ACCOUNT
            or self.fixture["prefix"] != prefix
            or self.config["account_id"] != ACCOUNT
            or self.config["region"] != REGION
        ):
            raise RuntimeError("WrongTestAccount")
        expected_path = (
            f"arn:aws:iam::{ACCOUNT}:role/vector-test/bastion-automation/{prefix}"
        )
        if (
            self.fixture["operations_arn"] != expected_path + "-operations"
            or self.fixture["investigator_arn"] != expected_path + "-readonly"
            or self.config["execution_role_arn"]
            != expected_path + "-bastion-automation"
        ):
            raise RuntimeError("WrongFixtureRoles")
        readonly = self.admin("sts").assume_role(
            RoleArn=self.fixture["investigator_arn"],
            RoleSessionName="automation-readonly",
            DurationSeconds=3600,
        )["Credentials"]
        self.creds = self.client("sts", readonly).assume_role(
            RoleArn=self.fixture["operations_arn"],
            RoleSessionName="automation-operator",
            DurationSeconds=3600,
        )["Credentials"]
        self.cli = AwsCli(self.creds)
        self.cli_config = Configuration(
            account=ACCOUNT, role=self.fixture["operations_name"], prefix=prefix
        )
        verify_identity(self.cli, account=ACCOUNT, role=self.fixture["operations_name"])

    def admin(self, service):
        return self.session.create_client(service, region_name=REGION, config=OPTIONS)

    def client(self, service, creds=None):
        creds = creds or self.creds
        return self.session.create_client(
            service,
            region_name=REGION,
            config=OPTIONS,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )

    def start(self, action="create", instance_id=None, **overrides):
        args = {
            "DocumentName": self.config[f"{action}_document"],
            "DocumentVersion": self.config[f"{action}_version"],
            "ClientToken": str(uuid.uuid4()),
        }
        if instance_id:
            args["Parameters"] = {"InstanceId": [instance_id]}
        args.update(overrides)
        for attempt in range(12):
            try:
                result = self.client("ssm").start_automation_execution(**args)
                break
            except ClientError as error:
                if (
                    error.response["Error"]["Code"] != "AccessDeniedException"
                    or attempt == 11
                ):
                    raise
                time.sleep(5)

        execution_id = result["AutomationExecutionId"]
        record("started", action=action, execution_id=execution_id)
        return execution_id

    def wait(self, execution_id, timeout=1000):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            result = self.client("ssm").get_automation_execution(
                AutomationExecutionId=execution_id
            )["AutomationExecution"]
            summary = (
                result["AutomationExecutionStatus"],
                tuple(
                    (step["StepName"], step["StepStatus"])
                    for step in result.get("StepExecutions", [])
                ),
            )
            if summary != last:
                record(
                    "execution",
                    execution_id=execution_id,
                    status=summary[0],
                    steps=summary[1],
                )
                last = summary
            if summary[0] in {
                "Success",
                "Failed",
                "TimedOut",
                "Cancelled",
                "Rejected",
                "CompletedWithSuccess",
                "CompletedWithFailure",
                "Exited",
            }:
                # ローカル診断はエラーの固定ラベルとIDに絞る。
                for step in result.get("StepExecutions", []):
                    if step["StepStatus"] == "Failed":
                        labels = re.findall(
                            r"(?:LifecycleError|RuntimeError): ([A-Za-z0-9_]+)",
                            step.get("FailureMessage", ""),
                        )
                        record(
                            "failed_step",
                            execution_id=execution_id,
                            step=step["StepName"],
                            labels=labels,
                        )
                return result
            time.sleep(5)
        raise RuntimeError("ExecutionWaitDeadline")

    def stop_admin(self, execution_id):
        value = self.admin("ssm").get_automation_execution(
            AutomationExecutionId=execution_id
        )["AutomationExecution"]
        if value["AutomationExecutionStatus"] in {
            "Success",
            "Failed",
            "Cancelled",
            "TimedOut",
            "CompletedWithSuccess",
            "CompletedWithFailure",
            "Exited",
        }:
            return value
        if value.get("Mode") != "Interactive":
            self.admin("ssm").stop_automation_execution(
                AutomationExecutionId=execution_id, Type="Cancel"
            )
            return self.wait(execution_id)
        if not value["DocumentName"].startswith(self.fixture["prefix"]):
            raise RuntimeError("OutsideInteractiveExecution")
        policy_name = "test-only-stop-interactive"
        # Interactiveの終了は開始者に限られるため、この実行だけを一時許可する。
        self.admin("iam").put_role_policy(
            RoleName=self.fixture["operations_name"],
            PolicyName=policy_name,
            PolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": "ssm:StopAutomationExecution",
                            "Resource": f"arn:aws:ssm:{REGION}:{ACCOUNT}:automation-execution/{execution_id}",
                        }
                    ],
                }
            ),
        )
        try:
            for attempt in range(12):
                try:
                    Probe().client("ssm").stop_automation_execution(
                        AutomationExecutionId=execution_id, Type="Cancel"
                    )
                    return self.wait(execution_id)
                except ClientError as error:
                    if (
                        error.response["Error"]["Code"] != "AccessDeniedException"
                        or attempt == 11
                    ):
                        raise
                    time.sleep(5)
        finally:
            self.admin("iam").delete_role_policy(
                RoleName=self.fixture["operations_name"], PolicyName=policy_name
            )

    def current(self):
        return status(self.cli, self.cli_config)

    def positive(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _: self.start(), range(2)))
        values = [self.wait(value) for value in ids]
        if any(value["AutomationExecutionStatus"] != "Success" for value in values):
            raise RuntimeError("ConcurrentCreateFailed")
        instance_id = self.current()["instance_id"]
        for value in values:
            if value["Outputs"].get("create.InstanceId") != [instance_id]:
                raise RuntimeError("DifferentInstances")
        active = self.admin("ec2").describe_instances(
            Filters=[
                {
                    "Name": "tag:vector:managed-bastion",
                    "Values": [self.fixture["prefix"]],
                },
                {
                    "Name": "instance-state-name",
                    "Values": [
                        "pending",
                        "running",
                        "stopping",
                        "stopped",
                        "shutting-down",
                    ],
                },
            ]
        )
        if sum(len(r["Instances"]) for r in active["Reservations"]) != 1:
            raise RuntimeError("TooManyInstances")
        record("concurrent_create_and_reuse", passed=True, instance_id=instance_id)
        reused = self.wait(self.start())
        if reused["AutomationExecutionStatus"] != "Success" or reused["Outputs"].get(
            "create.InstanceId"
        ) != [instance_id]:
            raise RuntimeError("ReuseFailed")
        marker = "automation-test-" + str(uuid.uuid4())
        self.admin("sqs").send_message(
            QueueUrl=self.fixture["dlq_url"], MessageBody=marker
        )
        pair = QueuePair(
            self.fixture["source_arn"],
            self.fixture["dlq_arn"],
            self.fixture["source_url"],
            self.fixture["dlq_url"],
        )
        with sqs_tunnel(
            self.cli, instance_id, self.fixture["tunnel_document"]
        ) as proxy:
            rejected = self.wait(self.start("destroy", instance_id))
            if rejected["AutomationExecutionStatus"] != "Failed":
                raise RuntimeError("ActiveSessionWasTerminated")
            record("active_session_destroy_rejected", passed=True)
            started_at = time.time() - 2
            result = operate(self.cli, proxy, pair, "start")
            record("redrive_started", task_handle=result["task_handle"])
            for _ in range(36):
                tasks = operate(self.cli, proxy, pair, "status")["tasks"]
                if any(
                    task.get("TaskHandle") in {None, result["task_handle"]}
                    and task.get("Status") == "COMPLETED"
                    and (
                        task.get("StartedTimestamp", 0)
                        / (
                            1000
                            if task.get("StartedTimestamp", 0) > 100_000_000_000
                            else 1
                        )
                    )
                    >= started_at
                    for task in tasks
                ):
                    break
                time.sleep(5)
            else:
                raise RuntimeError("RedriveNotComplete")
            self.cli.request(
                "sqs",
                "send-message",
                proxy_url=proxy,
                queue_url=self.fixture["source_url"],
                message_body="direct-send-allowed",
            )
        deadline = time.monotonic() + 90
        found = False
        while time.monotonic() < deadline and not found:
            messages = (
                self.admin("sqs")
                .receive_message(
                    QueueUrl=self.fixture["source_url"],
                    MaxNumberOfMessages=10,
                    WaitTimeSeconds=10,
                )
                .get("Messages", [])
            )
            for message in messages:
                found = found or message["Body"] == marker
                self.admin("sqs").delete_message(
                    QueueUrl=self.fixture["source_url"],
                    ReceiptHandle=message["ReceiptHandle"],
                )
        if not found:
            raise RuntimeError("RedriveMessageMissing")
        record(
            "fixed_tunnel_redrive_and_direct_send", passed=True, instance_id=instance_id
        )
        self.destroy_and_recreate(instance_id)

    def destroy_and_recreate(self, instance_id):
        value = self.wait(self.start("destroy", instance_id))
        if value["AutomationExecutionStatus"] != "Success":
            raise RuntimeError("DestroyFailed")
        if self.current()["instance_id"] is not None:
            raise RuntimeError("InterfaceNotReleased")
        value = self.wait(self.start())
        if value["AutomationExecutionStatus"] != "Success":
            raise RuntimeError("RecreateFailed")
        new_id = self.current()["instance_id"]
        if new_id == instance_id:
            raise RuntimeError("DidNotRecreate")
        record(
            "same_eni_recreated",
            passed=True,
            old_instance=instance_id,
            new_instance=new_id,
        )

    def denied(self, name, service, method, **kwargs):
        try:
            getattr(self.client(service), method)(**kwargs)
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code not in {
                "AccessDenied",
                "AccessDeniedException",
                "UnauthorizedOperation",
                "InvalidAutomationExecutionParametersException",
                "ValidationException",
                "InvalidDocumentVersion",
                "InvalidDocument",
            }:
                raise
            record(name, passed=True, error=code)
            return
        raise RuntimeError("UnexpectedAllow_" + name)

    def negative(self):
        reused = cli_start(
            self.cli,
            self.cli_config,
            "create",
            emit=lambda value: record("cli_started", **json.loads(value)),
        )
        if reused["status"] != "Success" or reused["instance_ids"] != [
            self.current()["instance_id"]
        ]:
            raise RuntimeError("CliCreateFailed")
        record("production_cli_create_reuse", passed=True)
        c = self.config
        self.denied(
            "direct_launch",
            "ec2",
            "run_instances",
            LaunchTemplate={
                "LaunchTemplateId": c["template_id"],
                "Version": c["template_version"],
            },
            MinCount=1,
            MaxCount=1,
            DryRun=True,
        )
        self.denied(
            "edit_template",
            "ec2",
            "create_launch_template_version",
            LaunchTemplateId=c["template_id"],
            SourceVersion=c["template_version"],
            LaunchTemplateData={"UserData": "ZWNobyB0ZXN0"},
            DryRun=True,
        )
        self.denied(
            "assume_executor",
            "sts",
            "assume_role",
            RoleArn=c["execution_role_arn"],
            RoleSessionName="forbidden-executor",
        )
        self.denied(
            "extra_userdata",
            "ssm",
            "start_automation_execution",
            DocumentName=c["create_document"],
            DocumentVersion=c["create_version"],
            Parameters={"UserData": ["test"]},
        )
        self.denied(
            "other_runbook",
            "ssm",
            "start_automation_execution",
            DocumentName="AWS-StartEC2Instance",
            Parameters={"InstanceId": [self.current()["instance_id"]]},
        )
        self.denied(
            "unapproved_version",
            "ssm",
            "start_automation_execution",
            DocumentName=c["create_document"],
            DocumentVersion=str(int(c["create_version"]) + 1),
        )
        self.denied(
            "edit_document",
            "ssm",
            "update_document",
            Name=c["create_document"],
            DocumentVersion="$LATEST",
            Content=json.dumps(
                {"schemaVersion": "0.3", "description": "forbidden", "mainSteps": []}
            ),
        )
        self.denied(
            "public_queue_send",
            "sqs",
            "send_message",
            QueueUrl=self.fixture["source_url"],
            MessageBody="forbidden-public",
        )
        instance_id = self.current()["instance_id"]
        self.denied(
            "direct_terminate",
            "ec2",
            "terminate_instances",
            InstanceIds=[instance_id],
            DryRun=True,
        )
        self.denied(
            "direct_userdata_edit",
            "ec2",
            "modify_instance_attribute",
            InstanceId=instance_id,
            UserData={"Value": b"echo test"},
            DryRun=True,
        )
        self.denied(
            "direct_tag_edit",
            "ec2",
            "create_tags",
            Resources=[instance_id],
            Tags=[{"Key": "vector:managed-bastion", "Value": "forbidden"}],
            DryRun=True,
        )
        self.denied(
            "shell_session",
            "ssm",
            "start_session",
            Target=instance_id,
            DocumentName="SSM-SessionManagerRunShell",
        )
        self.denied("default_shell", "ssm", "start_session", Target=instance_id)
        self.denied(
            "generic_forward",
            "ssm",
            "start_session",
            Target=instance_id,
            DocumentName="AWS-StartPortForwardingSessionToRemoteHost",
            Parameters={
                "host": ["example.com"],
                "portNumber": ["443"],
                "localPortNumber": ["18444"],
            },
        )
        self.denied(
            "outside_queue_send",
            "sqs",
            "send_message",
            QueueUrl=f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/{self.fixture['prefix']}-outside",
            MessageBody="forbidden-outside",
        )
        for label, overrides in [
            (
                "extra_targets",
                {
                    "TargetParameterName": "InstanceId",
                    "Targets": [{"Key": "ParameterValues", "Values": [instance_id]}],
                },
            ),
            (
                "target_locations",
                {
                    "TargetLocations": [
                        {
                            "Accounts": [ACCOUNT],
                            "Regions": [REGION],
                            "ExecutionRoleName": c["execution_role_arn"].rsplit("/", 1)[
                                1
                            ],
                        }
                    ]
                },
            ),
        ]:
            try:
                execution_id = self.start(**overrides)
            except ClientError as error:
                record(label, result=error.response["Error"]["Code"])
            else:
                result = self.wait(execution_id)
                if self.current()["instance_id"] != instance_id:
                    raise RuntimeError("TargetOverrideChangedInstance")
                record(
                    label,
                    result=result["AutomationExecutionStatus"],
                    fixed_instance_preserved=True,
                )
        readonly = self.admin("sts").assume_role(
            RoleArn=self.fixture["investigator_arn"],
            RoleSessionName="other-readonly",
            DurationSeconds=3600,
        )["Credentials"]
        other = self.client("sts", readonly).assume_role(
            RoleArn=self.fixture["operations_arn"],
            RoleSessionName="other-operator",
            DurationSeconds=3600,
        )["Credentials"]
        session_id = self.client("ssm").start_session(
            Target=instance_id, DocumentName=self.fixture["tunnel_document"]
        )["SessionId"]
        try:
            try:
                self.client("ssm", other).terminate_session(SessionId=session_id)
            except ClientError as error:
                if error.response["Error"]["Code"] != "AccessDeniedException":
                    raise
                record("other_owner_cannot_end_session", passed=True)
            else:
                raise RuntimeError("OtherOwnerTerminatedSession")
        finally:
            self.client("ssm").terminate_session(SessionId=session_id)
        # 別名や省略が受理されても、承認済み数値版へ解決されたことを検査する。
        for alias in [None, "$DEFAULT", "$LATEST"]:
            args = {"DocumentName": c["create_document"]}
            if alias:
                args["DocumentVersion"] = alias
            try:
                response = self.client("ssm").start_automation_execution(**args)
            except ClientError as error:
                record(
                    "version_alias", alias=alias, result=error.response["Error"]["Code"]
                )
            else:
                result = self.wait(response["AutomationExecutionId"])
                if result["DocumentVersion"] != c["create_version"]:
                    raise RuntimeError("UnapprovedVersionResolved")
                record(
                    "version_alias",
                    alias=alias,
                    result="approved-version-only",
                    version=result["DocumentVersion"],
                )
        execution_id = self.start(Mode="Interactive")
        self.denied(
            "interactive_signal",
            "ssm",
            "send_automation_signal",
            AutomationExecutionId=execution_id,
            SignalType="StartStep",
            Payload={"StepName": ["create"]},
        )
        self.stop_admin(execution_id)
        record("interactive_cannot_advance", passed=True)

    def finish(self):
        current = self.current()
        if current.get("instance_id"):
            value = cli_start(
                self.cli,
                self.cli_config,
                "destroy",
                current["instance_id"],
                emit=lambda value: record("cli_started", **json.loads(value)),
            )
            if value["status"] != "Success":
                raise RuntimeError("FinalDestroyFailed")
        record("daily_resources_removed", passed=True, status=self.current())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["positive", "negative", "finish"])
    args = parser.parse_args()
    try:
        getattr(Probe(), args.phase)()
    except Exception as error:
        record(
            "probe_failed",
            kind=type(error).__name__,
            code=str(error) if isinstance(error, RuntimeError) else "InspectLocally",
        )
        raise SystemExit(1)
