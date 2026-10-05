"""隔離したSQSで再投入と直接送信の権限を実測する。"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time

ROOT = Path(__file__).resolve().parent
ACCOUNT = "733360597472"
REGION = "ap-northeast-1"


class AwsFailure(RuntimeError):
    def __init__(self, output):
        super().__init__(output.strip())
        match = re.search(r"An error occurred \(([^)]+)\)", output)
        self.code = match.group(1) if match else "CLIError"


def aws(profile, service, operation, *, credentials=None, proxy_url=None, **arguments):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AWS_") and not key.lower().endswith("_proxy")
    }
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    if proxy_url is not None:
        if service != "sqs" or not re.fullmatch(
            r"http://127\.0\.0\.1:[0-9]{1,5}", proxy_url
        ):
            raise ValueError("proxyはSQS専用のloopback宛てだけを許可します。")
        env["HTTPS_PROXY"] = proxy_url
        env["NO_PROXY"] = ""
    command = [
        "aws",
        "--region",
        "us-east-1" if service == "iam" else REGION,
        "--output",
        "json",
        "--no-cli-pager",
        "--endpoint-url",
        (
            "https://iam.amazonaws.com"
            if service == "iam"
            else f"https://{service}.{REGION}.amazonaws.com"
        ),
    ]
    if credentials is None:
        command += ["--profile", profile]
    else:
        env.update(
            {
                "AWS_ACCESS_KEY_ID": credentials["AccessKeyId"],
                "AWS_SECRET_ACCESS_KEY": credentials["SecretAccessKey"],
                "AWS_SESSION_TOKEN": credentials["SessionToken"],
            }
        )
    command += [service, operation]
    for key, value in arguments.items():
        command += ["--" + key.replace("_", "-"), str(value)]
    result = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=45
    )
    if result.returncode:
        raise AwsFailure(result.stderr)
    return json.loads(result.stdout or "{}")


def receive(profile, url, visibility=0):
    return aws(
        profile,
        "sqs",
        "receive-message",
        queue_url=url,
        max_number_of_messages=1,
        wait_time_seconds=1,
        visibility_timeout=visibility,
    ).get("Messages", [])


def marker(fixture, name):
    return json.dumps({"test": "dlq-redrive", "run": fixture["prefix"], "case": name})


def seed(profile, fixture):
    for name, case in fixture["cases"].items():
        for url in (case["source_url"], case["dlq_url"]):
            attributes = aws(
                profile,
                "sqs",
                "get-queue-attributes",
                queue_url=url,
                attribute_names="All",
            )["Attributes"]
            if any(
                int(attributes[key])
                for key in (
                    "ApproximateNumberOfMessages",
                    "ApproximateNumberOfMessagesNotVisible",
                    "ApproximateNumberOfMessagesDelayed",
                )
            ):
                raise RuntimeError("seedは空の新規キューにだけ実行してください。")
        body = marker(fixture, name)
        aws(
            profile,
            "sqs",
            "send-message",
            queue_url=case["source_url"],
            message_body=body,
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            receive(profile, case["source_url"])
            messages = receive(profile, case["dlq_url"])
            if messages:
                if messages[0]["Body"] != body:
                    raise RuntimeError("DLQのメッセージが試験データと一致しません。")
                print(f"{name}: source -> DLQ confirmed", flush=True)
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"{name}: DLQへの移動が時間内に確認できません。")


def attempt(profile, credentials, operation, **arguments):
    try:
        value = aws(profile, "sqs", operation, credentials=credentials, **arguments)
        return {"allowed": True, "value": value}
    except AwsFailure as error:
        if error.code not in {"AccessDenied", "AccessDeniedException"}:
            raise
        return {"allowed": False, "code": error.code, "message": str(error)}


def assume_operations(profile, fixture, name):
    credentials = None
    if name == "queue-called-via" and fixture.get("investigator_arn"):
        expected = f"arn:aws:iam::{ACCOUNT}:role/vector-test/dlq-redrive/{fixture['prefix']}-investigator"
        if fixture["investigator_arn"] != expected:
            raise RuntimeError("調査ロールのARNが試験対象ではありません。")
        credentials = aws(
            profile,
            "sts",
            "assume-role",
            role_arn=expected,
            role_session_name="dlq-investigator",
            duration_seconds=3600,
        )["Credentials"]
    return aws(
        profile,
        "sts",
        "assume-role",
        credentials=credentials,
        role_arn=fixture["cases"][name]["role_arn"],
        role_session_name="dlq-redrive-probe",
        duration_seconds=3600,
    )["Credentials"]


def verify(profile, fixture, report_path, proxy_url=None):
    report = {
        "account_id": ACCOUNT,
        "prefix": fixture["prefix"],
        "private_connection": fixture.get("private_connection"),
        "via_proxy": proxy_url is not None,
        "cases": {},
    }
    try:
        for name in ("control", "current", "queue-called-via", "queue-via-service"):
            case = fixture["cases"][name]
            credentials = assume_operations(profile, fixture, name)
            identity = aws(
                profile, "sts", "get-caller-identity", credentials=credentials
            )
            expected = f"arn:aws:sts::{ACCOUNT}:assumed-role/{case['role_name']}/dlq-redrive-probe"
            if identity["Arn"] != expected:
                raise RuntimeError("実行ロールが試験ロールと一致しません。")
            previous = aws(
                profile,
                "sqs",
                "list-message-move-tasks",
                credentials=credentials,
                proxy_url=proxy_url,
                source_arn=case["dlq_arn"],
                max_results=10,
            ).get("Results", [])
            if previous:
                raise RuntimeError("verifyは新規キューで1回だけ実行してください。")
            result = {
                "caller": identity["Arn"],
                "queue_attributes": aws(
                    profile,
                    "sqs",
                    "get-queue-attributes",
                    queue_url=case["source_url"],
                    attribute_names="All",
                )["Attributes"],
                "role_policy": aws(
                    profile,
                    "iam",
                    "get-role-policy",
                    role_name=case["role_name"],
                    policy_name="dlq-redrive",
                )["PolicyDocument"],
            }
            report["cases"][name] = result
            result["outside_send"] = attempt(
                profile,
                credentials,
                "send-message",
                proxy_url=proxy_url,
                queue_url=fixture["outside_url"],
                message_body="synthetic-outside-scope-probe",
            )
            if proxy_url is not None and name != "control":
                result["public_direct_send"] = attempt(
                    profile,
                    credentials,
                    "send-message",
                    queue_url=case["source_url"],
                    message_body="synthetic-public-send-probe",
                )
                if result["public_direct_send"]["allowed"]:
                    raise RuntimeError(f"{name}: 公開経路の直接送信が許可されました。")
            result["direct_send"] = attempt(
                profile,
                credentials,
                "send-message",
                proxy_url=proxy_url,
                queue_url=case["source_url"],
                message_body="synthetic-direct-send-probe",
            )
            result["redrive"] = attempt(
                profile,
                credentials,
                "start-message-move-task",
                proxy_url=proxy_url,
                source_arn=case["dlq_arn"],
                destination_arn=case["source_arn"],
                max_number_of_messages_per_second=1,
            )
            if result["redrive"]["allowed"]:
                handle = result["redrive"]["value"]["TaskHandle"]
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    tasks = aws(
                        profile,
                        "sqs",
                        "list-message-move-tasks",
                        credentials=credentials,
                        proxy_url=proxy_url,
                        source_arn=case["dlq_arn"],
                        max_results=10,
                    ).get("Results", [])
                    # 完了後はTaskHandleが返らないため、新規DLQの唯一のtaskを照合する。
                    if len(tasks) > 1:
                        raise RuntimeError("想定外のredrive taskを検出しました。")
                    task = tasks[0] if tasks else None
                    if task and (
                        task["SourceArn"] != case["dlq_arn"]
                        or task["DestinationArn"] != case["source_arn"]
                        or task.get("TaskHandle", handle) != handle
                    ):
                        raise RuntimeError("redrive taskの対象が一致しません。")
                    if task and task["Status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                        result["task"] = task
                        break
                    time.sleep(3)
                else:
                    aws(
                        profile,
                        "sqs",
                        "cancel-message-move-task",
                        credentials=credentials,
                        proxy_url=proxy_url,
                        task_handle=handle,
                    )
                    raise RuntimeError(f"{name}: redriveが時間内に終了しません。")
                if task["Status"] == "COMPLETED":
                    deadline = time.monotonic() + 30
                    found = False
                    while time.monotonic() < deadline:
                        messages = receive(profile, case["source_url"], visibility=30)
                        for message in messages:
                            if message["Body"] == marker(fixture, name):
                                found = True
                            elif message["Body"] != "synthetic-direct-send-probe":
                                raise RuntimeError("試験外のメッセージを検出しました。")
                            aws(
                                profile,
                                "sqs",
                                "delete-message",
                                queue_url=case["source_url"],
                                receipt_handle=message["ReceiptHandle"],
                            )
                        if found:
                            break
                    result["payload_returned"] = found
                    if not found:
                        raise RuntimeError(
                            f"{name}: redrive後のメッセージを確認できません。"
                        )
            print(json.dumps({name: result}, ensure_ascii=False), flush=True)
            if result["outside_send"]["allowed"]:
                raise RuntimeError(f"{name}: 対象外キューへの送信が許可されました。")
            expected_direct = (
                name != "current" if proxy_url is not None else name == "control"
            )
            if result["direct_send"]["allowed"] != expected_direct:
                raise RuntimeError(f"{name}: 直接送信の結果が期待と一致しません。")
            if name == "control" and not result.get("payload_returned"):
                raise RuntimeError("対照ケースの再投入に失敗したため比較を中断します。")
    finally:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("seed", "verify"))
    parser.add_argument("--profile", default="vector-test-admin")
    parser.add_argument("--via-vpce", action="store_true")
    parser.add_argument("--report", type=Path, default=ROOT / ".local/result.json")
    args = parser.parse_args()
    fixture = json.loads(
        subprocess.check_output(
            ["terraform", f"-chdir={ROOT}", "output", "-json", "fixture"],
            text=True,
        )
    )
    identity = aws(args.profile, "sts", "get-caller-identity")
    if fixture["account_id"] != ACCOUNT or identity["Account"] != ACCOUNT:
        raise RuntimeError("テスト専用アカウント以外では実行できません。")
    if fixture["phase"] != args.phase:
        raise RuntimeError("Terraformに適用したphaseと試験段階が一致しません。")
    prefix = fixture["prefix"]
    if not re.fullmatch(r"vector-test-dlq-[a-z0-9][a-z0-9-]{0,23}", prefix):
        raise RuntimeError("試験リソースの名前が不正です。")
    if set(fixture["cases"]) != {
        "current",
        "queue-called-via",
        "queue-via-service",
        "control",
    }:
        raise RuntimeError("試験ケースが一致しません。")
    for name, case in fixture["cases"].items():
        expected_name = f"{prefix}-{name}"
        for suffix, key in (("", "source_url"), ("-dlq", "dlq_url")):
            expected = (
                f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/{expected_name}{suffix}"
            )
            if case[key] != expected:
                raise RuntimeError("試験キューのURLが不正です。")
            arn_key = key.replace("_url", "_arn")
            if (
                case[arn_key]
                != f"arn:aws:sqs:{REGION}:{ACCOUNT}:{expected_name}{suffix}"
            ):
                raise RuntimeError("試験キューのARNが不正です。")
        if case["role_name"] != expected_name:
            raise RuntimeError("試験ロールの名前が不正です。")
        if (
            case["role_arn"]
            != f"arn:aws:iam::{ACCOUNT}:role/vector-test/dlq-redrive/{expected_name}"
        ):
            raise RuntimeError("試験ロールのARNが不正です。")
    if (
        fixture["outside_url"]
        != f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/{prefix}-outside"
    ):
        raise RuntimeError("対象外キューのURLが不正です。")
    if args.via_vpce:
        connection = fixture.get("private_connection")
        if args.phase != "verify" or not connection:
            raise RuntimeError("VPC経由の検証にはprivate_pathとverify段階が必要です。")
        if (
            not re.fullmatch(r"i-[0-9a-f]+", connection["instance_id"])
            or connection["document_name"] != f"{prefix}-sqs-tunnel"
        ):
            raise RuntimeError("トンネルの接続先が試験リソースと一致しません。")
        from tunnel import sqs_tunnel

        credentials = aws(
            args.profile,
            "sts",
            "assume-role",
            role_arn=fixture["cases"]["control"]["role_arn"],
            role_session_name="dlq-redrive-tunnel",
            duration_seconds=1800,
        )["Credentials"]
        with sqs_tunnel(args.profile, fixture, credentials, aws) as proxy_url:
            verify(args.profile, fixture, args.report, proxy_url=proxy_url)
    elif args.phase == "seed":
        seed(args.profile, fixture)
    else:
        verify(args.profile, fixture, args.report)


if __name__ == "__main__":
    main()
