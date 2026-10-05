"""対象5工程の再投入を運用ロールから実行する。"""

import argparse
from dataclasses import dataclass
import json
import re
import signal
import sys

from .aws_cli import AwsCli, AwsFailure, HOST, REGION, profile_credentials
from .tunnel import sqs_tunnel

ACCOUNT = "222753227567"
STAGES = ("acquisition", "completion", "curation", "assessment", "embedding")
COUNTS = (
    "ApproximateNumberOfMessages",
    "ApproximateNumberOfMessagesNotVisible",
    "ApproximateNumberOfMessagesDelayed",
)
TASK_FIELDS = (
    "TaskHandle",
    "Status",
    "SourceArn",
    "DestinationArn",
    "MaxNumberOfMessagesPerSecond",
    "ApproximateNumberOfMessagesMoved",
    "ApproximateNumberOfMessagesToMove",
    "StartedTimestamp",
)


@dataclass(frozen=True)
class QueuePair:
    source_arn: str
    dlq_arn: str
    source_url: str
    dlq_url: str

    @classmethod
    def for_stage(cls, stage):
        if stage not in STAGES:
            raise ValueError("対象外の工程です。")
        name = (
            "vector-source-acquisition"
            if stage == "acquisition"
            else f"vector-article-{stage}"
        )
        return cls(
            f"arn:aws:sqs:{REGION}:{ACCOUNT}:{name}",
            f"arn:aws:sqs:{REGION}:{ACCOUNT}:{name}-dlq",
            f"https://{HOST}/{ACCOUNT}/{name}",
            f"https://{HOST}/{ACCOUNT}/{name}-dlq",
        )


def verify_identity(client, account=ACCOUNT, role="vector-operations"):
    identity = client.request("sts", "get-caller-identity")
    expected = f"arn:aws:sts::{account}:assumed-role/{role}/"
    if identity.get("Account") != account or not identity.get("Arn", "").startswith(
        expected
    ):
        raise RuntimeError("呼び出し元が対象アカウントの運用ロールではありません。")


def operate(client, proxy_url, pair, action, task_handle=None):
    def sqs(operation, **kwargs):
        return client.request("sqs", operation, proxy_url=proxy_url, **kwargs)

    attributes = {}
    for name, url, arn in (
        ("source", pair.source_url, pair.source_arn),
        ("dlq", pair.dlq_url, pair.dlq_arn),
    ):
        value = sqs(
            "get-queue-attributes",
            queue_url=url,
            attribute_names=[
                "QueueArn",
                "RedrivePolicy",
                "SqsManagedSseEnabled",
                "KmsMasterKeyId",
                *COUNTS,
            ],
        )["Attributes"]
        if value.get("QueueArn") != arn:
            raise RuntimeError("実キューのARNが対象と一致しません。")
        if value.get("SqsManagedSseEnabled") != "true" or value.get("KmsMasterKeyId"):
            raise RuntimeError("対象キューの暗号化設定がSSE-SQSではありません。")
        attributes[name] = value
    try:
        redrive = json.loads(attributes["source"].get("RedrivePolicy", "{}"))
    except json.JSONDecodeError:
        raise RuntimeError("通常キューのDLQ設定を確認できません。") from None
    if redrive.get("deadLetterTargetArn") != pair.dlq_arn:
        raise RuntimeError("通常キューとDLQの対応が一致しません。")
    tasks = sqs("list-message-move-tasks", source_arn=pair.dlq_arn, max_results=10).get(
        "Results", []
    )
    result = {
        "source_arn": pair.source_arn,
        "dlq_arn": pair.dlq_arn,
        "counts": {
            name: {key: int(value[key]) for key in COUNTS}
            for name, value in attributes.items()
        },
    }
    if action == "status":
        result["tasks"] = [
            {key: task[key] for key in TASK_FIELDS if key in task} for task in tasks
        ]
    elif action == "start":
        if any(task.get("Status") in {"RUNNING", "CANCELLING"} for task in tasks):
            raise RuntimeError(
                "実行中または停止処理中のタスクがあります。statusで確認してください。"
            )
        try:
            started = sqs(
                "start-message-move-task",
                source_arn=pair.dlq_arn,
                destination_arn=pair.source_arn,
                max_number_of_messages_per_second=1,
            )
            handle = started.get("TaskHandle")
            if not isinstance(handle, str) or not handle:
                raise AwsFailure("MissingTaskHandle")
        except AwsFailure as error:
            raise RuntimeError(
                f"開始結果を確定できません ({error.code})。startを再実行せずstatusで確認してください。"
            ) from None
        result.update({"task_handle": handle, "messages_per_second": 1})
    elif action == "cancel":
        matches = [task for task in tasks if task.get("TaskHandle") == task_handle]
        if (
            len(matches) != 1
            or matches[0].get("Status") != "RUNNING"
            or matches[0].get("SourceArn") != pair.dlq_arn
            or matches[0].get("DestinationArn") not in {None, pair.source_arn}
        ):
            raise RuntimeError("指定ハンドルは対象DLQの実行中タスクではありません。")
        cancelled = sqs("cancel-message-move-task", task_handle=task_handle)
        result.update(
            {
                "task_handle": task_handle,
                "cancel_requested": True,
                "moved_before_cancel": cancelled.get(
                    "ApproximateNumberOfMessagesMoved"
                ),
            }
        )
    else:
        raise ValueError("未対応の操作です。")
    return result


def main():
    parser = argparse.ArgumentParser(description="一時踏み台経由で対象DLQを操作する。")
    parser.add_argument("action", choices=("status", "start", "cancel"))
    parser.add_argument("--stage", required=True, choices=STAGES)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--task-handle")
    args = parser.parse_args()
    if not re.fullmatch(r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})", args.instance_id):
        parser.error("踏み台IDの形式が不正です。")
    if (args.action == "cancel") != bool(args.task_handle):
        parser.error("--task-handleはcancelにだけ必須です。")
    pair = QueuePair.for_stage(args.stage)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        client = AwsCli(profile_credentials("vector-ops"))
        verify_identity(client)
        with sqs_tunnel(client, args.instance_id, "vector-sqs-tunnel") as proxy_url:
            result = operate(client, proxy_url, pair, args.action, args.task_handle)
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    except KeyboardInterrupt:
        print(
            "接続を終了しました。再投入タスクは自動停止しません。statusで確認してください。",
            file=sys.stderr,
        )
        return 130
    except (RuntimeError, OSError, ValueError, KeyError):
        # SDKやOS由来の詳細を露出させず、既知の運用エラーだけを表示する。
        error = sys.exc_info()[1]
        print(
            str(error)
            if isinstance(error, RuntimeError)
            else "操作に失敗しました。設定と接続を確認してください。",
            file=sys.stderr,
        )
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)
    return 0
