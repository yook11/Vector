"""承認された手順を開始し、資格情報を残さず結果を追跡する。"""

import argparse
from dataclasses import dataclass
import json
import re
import signal
import sys
import time
import uuid

from .aws_cli import AwsCli, AwsFailure, REGION, profile_credentials
from .redrive import ACCOUNT, verify_identity


@dataclass(frozen=True)
class Configuration:
    account: str = ACCOUNT
    role: str = "vector-operations"
    prefix: str = "vector"

    def document(self, action):
        return f"{self.prefix}-bastion-{action}"


def document(client, configuration, action):
    name = configuration.document(action)
    description = client.request("ssm", "describe-document", name=name)["Document"]
    version = description.get("DefaultVersion", "")
    if not re.fullmatch(r"[1-9][0-9]*", version):
        raise RuntimeError("公開された数値バージョンを取得できません。")
    value = client.request(
        "ssm",
        "get-document",
        name=name,
        document_version=version,
        document_format="JSON",
    )
    if value.get("DocumentVersion") != version:
        raise RuntimeError("ドキュメントのバージョンが一致しません。")
    content = json.loads(value["Content"])
    config = content["mainSteps"][0]["inputs"]["InputPayload"]["config"]
    if (
        config.get("account_id") != configuration.account
        or config.get("region") != REGION
    ):
        raise RuntimeError("手順の対象アカウント・リージョンが一致しません。")
    return version, config


def execution_status(client, configuration, execution_id):
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", execution_id):
        raise ValueError("実行IDの形式が不正です。")
    execution = client.request(
        "ssm", "get-automation-execution", automation_execution_id=execution_id
    )["AutomationExecution"]
    if execution.get("DocumentName") not in {
        configuration.document("create"),
        configuration.document("destroy"),
    }:
        raise RuntimeError("対象外のAutomation実行です。")
    instance_ids = {
        value
        for value in execution.get("Parameters", {}).get("InstanceId", [])
        if re.fullmatch(r"i-[0-9a-f]{17}", value)
    }
    for values in execution.get("Outputs", {}).values():
        instance_ids.update(
            value for value in values if re.fullmatch(r"i-[0-9a-f]{17}", value)
        )
    for step in execution.get("StepExecutions", []):
        instance_ids.update(
            value
            for value in step.get("Outputs", {}).get("InstanceId", [])
            if re.fullmatch(r"i-[0-9a-f]{17}", value)
        )
    return {
        "execution_id": execution_id,
        "document": execution["DocumentName"],
        "version": execution["DocumentVersion"],
        "status": execution["AutomationExecutionStatus"],
        "instance_ids": sorted(instance_ids),
        "steps": [
            {"name": step["StepName"], "status": step["StepStatus"]}
            for step in execution.get("StepExecutions", [])
        ],
    }


def status(client, configuration):
    _, config = document(client, configuration, "create")
    eni = client.request(
        "ec2",
        "describe-network-interfaces",
        network_interface_ids=[config["network"]["network_interface_id"]],
    )["NetworkInterfaces"][0]
    if (
        eni.get("OwnerId") != configuration.account
        or eni.get("VpcId") != config["network"]["vpc_id"]
    ):
        raise RuntimeError("固定ENIの所有者・VPCが一致しません。")
    instance_id = eni.get("Attachment", {}).get("InstanceId")
    if not instance_id:
        return {"instance_id": None, "network_interface_status": eni["Status"]}
    reservations = client.request(
        "ec2", "describe-instances", instance_ids=[instance_id]
    )["Reservations"]
    values = [
        value for reservation in reservations for value in reservation["Instances"]
    ]
    if len(values) != 1:
        raise RuntimeError("接続先EC2を確認できません。")
    instance = values[0]
    tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
    if (
        not all(tags.get(key) == value for key, value in config["tags"].items())
        or tags.get("aws:ec2launchtemplate:id") != config["template_id"]
    ):
        raise RuntimeError("固定ENIに想定外のEC2が接続されています。")
    managed = client.request(
        "ssm",
        "describe-instance-information",
        filters=json.dumps([{"Key": "InstanceIds", "Values": [instance_id]}]),
    ).get("InstanceInformationList", [])
    return {
        "instance_id": instance_id,
        "state": instance["State"]["Name"],
        "ssm": managed[0]["PingStatus"] if managed else "NotRegistered",
        "template_version": tags.get("aws:ec2launchtemplate:version"),
        "approved_template_version": config["template_version"],
    }


def start(
    client,
    configuration,
    action,
    instance_id=None,
    *,
    emit=print,
    clock=time.monotonic,
    sleep=time.sleep,
):
    version, _ = document(client, configuration, action)
    parameters = {}
    if action == "destroy":
        if not instance_id or not re.fullmatch(r"i-[0-9a-f]{17}", instance_id):
            raise ValueError("撤去対象のEC2 IDを明示してください。")
        parameters = {"InstanceId": [instance_id]}
    token = str(uuid.uuid4())
    emit(
        json.dumps(
            {
                "request_id": token,
                "document": configuration.document(action),
                "version": version,
            }
        )
    )
    try:
        result = client.request(
            "ssm",
            "start-automation-execution",
            document_name=configuration.document(action),
            document_version=version,
            client_token=token,
            mode="Auto",
            **({"parameters": json.dumps(parameters)} if parameters else {}),
        )
        execution_id = result["AutomationExecutionId"]
    except (AwsFailure, KeyError) as error:
        code = error.code if isinstance(error, AwsFailure) else "MissingExecutionId"
        if code in {
            "AccessDeniedException",
            "ValidationException",
            "InvalidDocument",
            "InvalidDocumentVersion",
            "InvalidAutomationExecutionParametersException",
        }:
            raise RuntimeError(
                f"開始が拒否されました ({code})。要求ID {token} の設定・権限を確認してください。"
            ) from None
        raise RuntimeError(
            f"開始結果は未確定です ({code})。要求ID {token} を管理者に伝えて実行履歴を照合してください。自動再実行はしません。"
        ) from None
    emit(
        json.dumps(
            {"execution_id": execution_id, "instance_id": instance_id},
            ensure_ascii=False,
        )
    )
    deadline = clock() + 780
    while clock() < deadline:
        value = execution_status(client, configuration, execution_id)
        if value["status"] in {
            "Success",
            "Failed",
            "TimedOut",
            "Cancelled",
            "Rejected",
            "CompletedWithSuccess",
            "CompletedWithFailure",
        }:
            return value
        sleep(5)
    return {
        "execution_id": execution_id,
        "status": "WaitingDeadlineReached",
        "follow_up": "status --execution-id " + execution_id,
    }


def main():
    parser = argparse.ArgumentParser(
        description="固定手順で一時踏み台を作成・撤去する。"
    )
    parser.add_argument("action", choices=("create", "status", "destroy"))
    parser.add_argument("--instance-id")
    parser.add_argument("--execution-id")
    args = parser.parse_args()
    if (args.action == "destroy") != bool(args.instance_id):
        parser.error("--instance-idはdestroyにだけ必須です。")
    if args.execution_id and args.action != "status":
        parser.error("--execution-idはstatusだけで使用します。")

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        config = Configuration()
        client = AwsCli(profile_credentials("vector-ops"))
        verify_identity(client, account=config.account, role=config.role)
        if args.action == "status":
            result = (
                execution_status(client, config, args.execution_id)
                if args.execution_id
                else status(client, config)
            )
        else:
            result = start(
                client,
                config,
                args.action,
                args.instance_id,
                emit=lambda value: print(value, flush=True),
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status", "Success") == "Success" else 1
    except KeyboardInterrupt:
        print(
            "待機を終了しました。Automationは続行します。表示された実行IDをstatusで確認してください。",
            file=sys.stderr,
        )
        return 130
    except (RuntimeError, ValueError, KeyError, OSError, TypeError) as error:
        print(
            str(error)
            if isinstance(error, RuntimeError)
            else "応答または入力を確認できません。表示された実行IDで照合してください。",
            file=sys.stderr,
        )
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
