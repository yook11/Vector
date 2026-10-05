"""backend既存のbotocoreでSSM接続の許可・拒否を確認する。"""

import argparse
import json
import time
from pathlib import Path
import re
import subprocess

import botocore.session
from botocore.exceptions import ClientError

from probe import ACCOUNT, REGION, ROOT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--report", type=Path, default=ROOT / ".local/operations/ssm-access.json"
    )
    args = parser.parse_args()
    fixture = json.loads(
        subprocess.check_output(
            ["terraform", f"-chdir={ROOT}", "output", "-json", "fixture"], text=True
        )
    )
    session = botocore.session.Session(profile="vector-test-admin")
    sts = session.create_client("sts", region_name=REGION)
    if (
        sts.get_caller_identity()["Account"] != ACCOUNT
        or fixture["account_id"] != ACCOUNT
    ):
        raise RuntimeError("テストアカウント以外では実行できません。")
    prefix = fixture["prefix"]
    if not re.fullmatch(r"vector-test-dlq-[a-z0-9][a-z0-9-]{0,23}", prefix):
        raise RuntimeError("試験prefixが不正です。")
    connection = fixture["private_connection"]
    if not connection or connection["document_name"] != f"{prefix}-sqs-tunnel":
        raise RuntimeError("試験用SSM documentが一致しません。")
    admin = session.create_client("ssm", region_name=REGION)
    ec2 = session.create_client("ec2", region_name=REGION)
    instance = ec2.describe_instances(InstanceIds=[connection["instance_id"]])[
        "Reservations"
    ][0]["Instances"][0]
    if {tag["Key"]: tag["Value"] for tag in instance["Tags"]}.get(
        "Name"
    ) != f"{prefix}-bastion":
        raise RuntimeError("試験用EC2ではありません。")
    clients = {}
    for name in ("control", "current"):
        role = fixture["cases"][name]["role_arn"]
        if (
            role
            != f"arn:aws:iam::{ACCOUNT}:role/vector-test/dlq-redrive/{prefix}-{name}"
        ):
            raise RuntimeError("試験運用ロールではありません。")
        credentials = sts.assume_role(
            RoleArn=role, RoleSessionName="dlq-ssm-guard", DurationSeconds=900
        )["Credentials"]
        clients[name] = session.create_client(
            "ssm",
            region_name=REGION,
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
        )
    report = {
        "account_id": ACCOUNT,
        "instance_id": connection["instance_id"],
        "cases": {},
    }
    target = {"Target": connection["instance_id"]}
    active = set()
    try:
        for label, value in (("missing_tag", None), ("wrong_tag", "other-purpose")):
            if value is None:
                ec2.delete_tags(
                    Resources=[connection["instance_id"]],
                    Tags=[{"Key": "vector:session-purpose"}],
                )
            else:
                ec2.create_tags(
                    Resources=[connection["instance_id"]],
                    Tags=[{"Key": "vector:session-purpose", "Value": value}],
                )
            time.sleep(5)
            try:
                created = clients["control"].start_session(
                    **target,
                    DocumentName=connection["document_name"],
                    Parameters={"localPortNumber": ["18443"]},
                )
            except ClientError as error:
                if error.response["Error"]["Code"] != "AccessDeniedException":
                    raise
                report["cases"][label] = "denied"
            else:
                active.add(created["SessionId"])
                raise RuntimeError(f"{label}: 許可外のタグで接続できました。")
        ec2.create_tags(
            Resources=[connection["instance_id"]],
            Tags=[{"Key": "vector:session-purpose", "Value": "sqs-redrive"}],
        )
        time.sleep(5)
        for name, arguments in {
            "default_shell": target,
            "explicit_shell": {**target, "DocumentName": "SSM-SessionManagerRunShell"},
            "arbitrary_forwarding": {
                **target,
                "DocumentName": "AWS-StartPortForwardingSessionToRemoteHost",
                "Parameters": {
                    "host": ["sqs.ap-northeast-1.amazonaws.com"],
                    "portNumber": ["443"],
                    "localPortNumber": ["18443"],
                },
            },
        }.items():
            try:
                created = clients["control"].start_session(**arguments)
            except ClientError as error:
                if error.response["Error"]["Code"] != "AccessDeniedException":
                    raise
                report["cases"][name] = "denied"
            else:
                active.add(created["SessionId"])
                report["cases"][name] = "unexpectedly_allowed"
                raise RuntimeError(f"{name}: 許可外の接続が成功しました。")
        created = clients["control"].start_session(
            **target,
            DocumentName=connection["document_name"],
            Parameters={"localPortNumber": ["18443"]},
        )
        session_id = created["SessionId"]
        active.add(session_id)
        report["cases"]["fixed_sqs_document"] = "allowed"
        try:
            clients["current"].terminate_session(SessionId=session_id)
        except ClientError as error:
            if error.response["Error"]["Code"] != "AccessDeniedException":
                raise
            report["cases"]["other_role_termination"] = "denied"
        else:
            report["cases"]["other_role_termination"] = "unexpectedly_allowed"
            raise RuntimeError("他ロールのセッション終了が許可されました。")
        clients["control"].terminate_session(SessionId=session_id)
        active.remove(session_id)
        report["cases"]["owner_termination"] = "allowed"
    finally:
        ec2.create_tags(
            Resources=[connection["instance_id"]],
            Tags=[{"Key": "vector:session-purpose", "Value": "sqs-redrive"}],
        )
        for session_id in active:
            admin.terminate_session(SessionId=session_id)
        output = args.report
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
