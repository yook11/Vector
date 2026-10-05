"""隔離環境で本番CLIと同じ接続・再投入・停止処理を検証する。"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

from probe import (
    ACCOUNT,
    REGION,
    ROOT,
    assume_operations,
    attempt,
    aws,
    marker,
    receive,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "aws/scripts"))
from sqs_operations.aws_cli import AwsCli  # noqa: E402
from sqs_operations.redrive import QueuePair, operate, verify_identity  # noqa: E402
from sqs_operations.tunnel import sqs_tunnel  # noqa: E402

PROFILE = "vector-test-admin"


def load_fixture():
    fixture = json.loads(
        subprocess.check_output(
            ["terraform", f"-chdir={ROOT}", "output", "-json", "fixture"], text=True
        )
    )
    identity = aws(PROFILE, "sts", "get-caller-identity")
    if (
        identity["Account"] != ACCOUNT
        or fixture["account_id"] != ACCOUNT
        or fixture["phase"] != "verify"
    ):
        raise RuntimeError("verify段階のテストアカウントだけで実行してください。")
    prefix = fixture["prefix"]
    if not re.fullmatch(r"vector-test-dlq-[a-z0-9][a-z0-9-]{0,23}", prefix):
        raise RuntimeError("試験prefixが不正です。")
    case = fixture["cases"]["queue-called-via"]
    name = f"{prefix}-queue-called-via"
    expected = {
        "source_arn": f"arn:aws:sqs:{REGION}:{ACCOUNT}:{name}",
        "dlq_arn": f"arn:aws:sqs:{REGION}:{ACCOUNT}:{name}-dlq",
        "source_url": f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/{name}",
        "dlq_url": f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/{name}-dlq",
        "role_arn": f"arn:aws:iam::{ACCOUNT}:role/vector-test/dlq-redrive/{name}",
        "role_name": name,
    }
    if (
        case != expected
        or fixture["outside_url"]
        != f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/{prefix}-outside"
    ):
        raise RuntimeError("試験キューかロールが一致しません。")
    connection = fixture["private_connection"]
    if connection["document_name"] != f"{prefix}-sqs-tunnel" or not re.fullmatch(
        r"i-[0-9a-f]{17}", connection["instance_id"]
    ):
        raise RuntimeError("試験接続先が不正です。")
    return fixture, case, connection


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("verify", "reconnect"))
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    fixture, case, connection = load_fixture()
    investigator_arn = f"arn:aws:iam::{ACCOUNT}:role/vector-test/dlq-redrive/{fixture['prefix']}-investigator"
    if fixture.get("investigator_arn") != investigator_arn:
        raise RuntimeError("調査ロールが試験対象ではありません。")
    investigator = aws(
        PROFILE,
        "sts",
        "assume-role",
        role_arn=investigator_arn,
        role_session_name="readonly-negative-probe",
        duration_seconds=900,
    )["Credentials"]
    observed = aws(
        PROFILE,
        "sqs",
        "get-queue-attributes",
        credentials=investigator,
        queue_url=case["dlq_url"],
        attribute_names="QueueArn",
    )
    if observed["Attributes"]["QueueArn"] != case["dlq_arn"]:
        raise RuntimeError("調査ロールの観測対象が不一致です。")
    denied = attempt(
        PROFILE,
        investigator,
        "start-message-move-task",
        source_arn=case["dlq_arn"],
        destination_arn=case["source_arn"],
        max_number_of_messages_per_second=1,
    )
    if denied["allowed"]:
        raise RuntimeError("調査ロールに再投入が許可されました。")
    bastion_role = f"{fixture['prefix']}-bastion"
    attached = aws(
        PROFILE, "iam", "list-attached-role-policies", role_name=bastion_role
    )["AttachedPolicies"]
    inline = aws(PROFILE, "iam", "list-role-policies", role_name=bastion_role)[
        "PolicyNames"
    ]
    if {p["PolicyArn"] for p in attached} != {
        "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
    } or inline:
        raise RuntimeError("踏み台のIAM権限がSSM専用ではありません。")
    credentials = assume_operations(PROFILE, fixture, "queue-called-via")
    client = AwsCli(credentials)
    verify_identity(client, account=ACCOUNT, role=case["role_name"])
    pair = QueuePair(
        **{key: case[key] for key in ("source_arn", "dlq_arn", "source_url", "dlq_url")}
    )
    policy = aws(
        PROFILE,
        "iam",
        "get-role-policy",
        role_name=case["role_name"],
        policy_name="sqs-tunnel",
    )["PolicyDocument"]
    report = {
        "account_id": ACCOUNT,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "instance_id": connection["instance_id"],
        "mode": args.mode,
        "tunnel_policy_sha256": hashlib.sha256(
            json.dumps(policy, sort_keys=True).encode()
        ).hexdigest(),
        "checks": {
            "investigator_observe": "passed",
            "investigator_redrive": "denied",
            "bastion_ssm_only": "passed",
        },
    }
    try:
        with sqs_tunnel(
            client, connection["instance_id"], connection["document_name"]
        ) as proxy:
            status = operate(client, proxy, pair, "status")
            report["checks"]["same_role_tunnel_and_status"] = "passed"
            if args.mode == "reconnect":
                report["counts"] = status["counts"]
                return
            if status["tasks"]:
                raise RuntimeError(
                    "初回の再投入試験はタスク履歴のない新規DLQに限定します。"
                )
            started = operate(client, proxy, pair, "start")
            report["start"] = started
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                tasks = client.request(
                    "sqs",
                    "list-message-move-tasks",
                    proxy_url=proxy,
                    source_arn=pair.dlq_arn,
                    max_results=10,
                ).get("Results", [])
                if tasks and tasks[0]["Status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                    if tasks[0]["Status"] != "COMPLETED":
                        raise RuntimeError("再投入が正常完了しませんでした。")
                    report["checks"]["redrive_completed"] = "passed"
                    break
                time.sleep(2)
            else:
                raise RuntimeError("再投入の完了確認がタイムアウトしました。")
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                messages = receive(PROFILE, pair.source_url, visibility=10)
                if messages:
                    if messages[0]["Body"] != marker(fixture, "queue-called-via"):
                        raise RuntimeError("移送されたダミー本文が一致しません。")
                    aws(
                        PROFILE,
                        "sqs",
                        "delete-message",
                        queue_url=pair.source_url,
                        receipt_handle=messages[0]["ReceiptHandle"],
                    )
                    report["checks"]["original_payload_returned"] = "passed"
                    break
            else:
                raise RuntimeError("ダミー本文の到着を確認できません。")
            for name, url, through_proxy, expected in (
                ("vpce_direct_send", pair.source_url, proxy, True),
                ("public_direct_send", pair.source_url, None, False),
                ("outside_queue_send", fixture["outside_url"], proxy, False),
            ):
                result = attempt(
                    PROFILE,
                    credentials,
                    "send-message",
                    proxy_url=through_proxy,
                    queue_url=url,
                    message_body="synthetic-operations-direct-send",
                )
                if result["allowed"] != expected:
                    raise RuntimeError(f"{name}の認可結果が不一致です。")
                report["checks"][name] = "allowed" if expected else "denied"
            # 停止前に完了しない件数のダミーを試験DLQへ送る。
            for batch in range(10):
                entries = [
                    {"Id": str(i), "MessageBody": f"synthetic-cancel-{batch}-{i}"}
                    for i in range(10)
                ]
                sent = aws(
                    PROFILE,
                    "sqs",
                    "send-message-batch",
                    queue_url=pair.dlq_url,
                    entries=json.dumps(entries),
                )
                if sent.get("Failed"):
                    raise RuntimeError("キャンセル試験のダミー投入に失敗しました。")
            started = operate(client, proxy, pair, "start")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                tasks = client.request(
                    "sqs",
                    "list-message-move-tasks",
                    proxy_url=proxy,
                    source_arn=pair.dlq_arn,
                    max_results=10,
                ).get("Results", [])
                if any(
                    task.get("TaskHandle") == started["task_handle"]
                    and task["Status"] == "RUNNING"
                    for task in tasks
                ):
                    break
                time.sleep(1)
            else:
                raise RuntimeError("キャンセル対象のRUNNINGを確認できません。")
            report["cancel"] = operate(
                client, proxy, pair, "cancel", started["task_handle"]
            )
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                tasks = client.request(
                    "sqs",
                    "list-message-move-tasks",
                    proxy_url=proxy,
                    source_arn=pair.dlq_arn,
                    max_results=10,
                ).get("Results", [])
                if tasks and tasks[0]["Status"] == "CANCELLED":
                    report["checks"]["running_task_cancelled"] = "passed"
                    break
                if tasks and tasks[0]["Status"] in {"COMPLETED", "FAILED"}:
                    raise RuntimeError("キャンセル前にタスクが終了しました。")
                time.sleep(2)
            else:
                raise RuntimeError("キャンセル完了を確認できません。")
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
