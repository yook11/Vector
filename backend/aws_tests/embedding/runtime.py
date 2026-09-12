"""手元のRunner権限でSSM操作とLambdaの完了ログを照合する。"""

import base64
import json
import re
import shlex
import time
from contextlib import ExitStack, closing
from pathlib import Path

from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.session import Session


class EmbeddingRuntime:
    def __init__(self, outputs, expected_account, profile):
        self.outputs = outputs
        self.run = outputs["run"]
        self.execution = outputs["execution"]
        self.database = outputs["database"]
        self.evidence = []
        self.deadline = time.monotonic() + 300
        self.stack = ExitStack()
        prefix = f"vector-test-{self.run['run_id']}"
        if not re.fullmatch(r"[0-9]{12}", expected_account):
            raise ValueError("expected_account_invalid")
        if (
            self.run["account_id"] != expected_account
            or self.run["region"] != "ap-northeast-1"
            or not re.fullmatch(r"[a-z0-9-]{1,40}", self.run["run_id"])
            or self.database["identifier"] != prefix
            or self.database["name"] != "vector"
            or self.database["port"] != 5432
            or not self.database["address"].startswith(prefix + ".")
            or not self.database["address"].endswith(
                ".ap-northeast-1.rds.amazonaws.com"
            )
            or self.execution["queue_url"]
            != (
                f"https://sqs.ap-northeast-1.amazonaws.com/{expected_account}/{prefix}-embedding"
            )
            or self.execution["log_groups"]["lambda"] != f"/vector-test/{prefix}/lambda"
            or self.execution["log_groups"]["runner"] != f"/vector-test/{prefix}/runner"
        ):
            raise ValueError("test_resource_scope_mismatch")
        image = self.run["backend_image"]
        registry = f"{expected_account}.dkr.ecr.ap-northeast-1.amazonaws.com"
        if not re.fullmatch(
            re.escape(registry) + r"/vector-test/backend@sha256:[a-f0-9]{64}", image
        ):
            raise ValueError("test_image_digest_required")
        self.instance_id = self.execution["instance_ids"]["runner"]
        if not re.fullmatch(r"i-[a-f0-9]{17}", self.instance_id):
            raise ValueError("runner_instance_id_invalid")
        session = Session(profile=profile)
        config = Config(
            connect_timeout=3,
            read_timeout=5,
            retries={"total_max_attempts": 1},
            ignore_configured_endpoint_urls=True,
        )
        try:
            with closing(
                session.create_client(
                    "sts", region_name="ap-northeast-1", config=config
                )
            ) as sts:
                identity = sts.get_caller_identity()
            if (
                identity["Account"] != expected_account
                or ":assumed-role/AWSReservedSSO_VectorTestRunner_"
                not in identity["Arn"]
            ):
                raise ValueError("test_runner_identity_required")
            self.ssm = self.stack.enter_context(
                closing(
                    session.create_client(
                        "ssm", region_name="ap-northeast-1", config=config
                    )
                )
            )
            self.logs = self.stack.enter_context(
                closing(
                    session.create_client(
                        "logs", region_name="ap-northeast-1", config=config
                    )
                )
            )
        except Exception:
            self.stack.close()
            raise

    def close(self):
        self.stack.close()

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("aws_smoke_deadline_exceeded")
        return remaining

    def pause(self):
        time.sleep(min(2, self.remaining()))

    def probe(self, operation, **values):
        self.remaining()
        payload = json.dumps(
            {
                "operation": operation,
                "database": self.database,
                "run_id": self.run["run_id"],
                "queue_url": self.execution["queue_url"],
                **values,
            }
        )
        source = base64.b64encode(
            Path(__file__).with_name("probe.py").read_bytes()
        ).decode()
        # hostネットワークでIMDSv2へ到達し、runnerのIAM認証をそのまま使う。
        command = "\n".join(
            [
                "set -eu",
                "probe_dir=$(mktemp -d /var/lib/vector-test/probe.XXXXXX)",
                "trap 'rm -rf \"$probe_dir\"' EXIT",
                'chmod 755 "$probe_dir"',
                f'printf %s {shlex.quote(source)} | base64 -d > "$probe_dir/probe.py"',
                'chmod 644 "$probe_dir/probe.py"',
                "timeout 45 docker run --rm --pull=never --network host "
                "--env-file /etc/vector-test/proxy.env --env PYTHONPATH=/app "
                '--mount "type=bind,src=$probe_dir/probe.py,'
                'dst=/tmp/probe.py,readonly" '
                "--workdir /app --entrypoint /app/.venv/bin/python "
                + shlex.quote(self.run["backend_image"])
                + " /tmp/probe.py "
                + shlex.quote(payload),
            ]
        )
        result = self.ssm.send_command(
            InstanceIds=[self.instance_id],
            DocumentName="AWS-RunShellScript",
            TimeoutSeconds=60,
            Parameters={"commands": [command], "executionTimeout": ["50"]},
            CloudWatchOutputConfig={
                "CloudWatchOutputEnabled": True,
                "CloudWatchLogGroupName": self.execution["log_groups"]["runner"],
            },
        )
        command_id = result["Command"]["CommandId"]
        self.evidence.append({"operation": operation, "command_id": command_id})
        while True:
            self.remaining()
            try:
                invocation = self.ssm.get_command_invocation(
                    CommandId=command_id, InstanceId=self.instance_id
                )
            except ClientError as error:
                if error.response["Error"]["Code"] != "InvocationDoesNotExist":
                    raise
            else:
                status = invocation["Status"]
                if status == "Success":
                    if invocation["ResponseCode"] != 0:
                        raise RuntimeError(
                            f"probe_exit_failed:{operation}:{command_id}"
                        )
                    return json.loads(invocation["StandardOutputContent"])
                if status not in {"Pending", "InProgress", "Delayed"}:
                    raise RuntimeError(
                        f"probe_failed:{operation}:{status}:{command_id}"
                    )
            self.pause()

    def seed(self):
        return self.probe("seed")

    def read(self, event):
        return self.probe("read", article_id=event["payload"]["analyzed_article_id"])[
            "embedding"
        ]

    def deliver_and_wait(self, event, *, expected_reason):
        started = int(time.time() * 1000) - 1000
        message_id = self.probe("send", body=json.dumps(event, allow_nan=False))[
            "message_id"
        ]
        self.evidence.append({"message_id": message_id, "event_id": event["event_id"]})
        while True:
            token = None
            while True:
                self.remaining()
                page = self.logs.filter_log_events(
                    logGroupName=self.execution["log_groups"]["lambda"],
                    startTime=started,
                    filterPattern="{ $.message_id = " + json.dumps(message_id) + " }",
                    **({"nextToken": token} if token else {}),
                )
                for entry in page["events"]:
                    record = json.loads(entry["message"])
                    if record.get("message_id") != message_id:
                        continue
                    kind = record.get("event")
                    if kind == "embedding_message_completed":
                        if (
                            record.get("event_id") != event["event_id"]
                            or record.get("analyzed_article_id")
                            != event["payload"]["analyzed_article_id"]
                            or record.get("reason") != expected_reason
                        ):
                            raise AssertionError("embedding_completion_mismatch")
                        self.evidence.append(
                            {"message_id": message_id, "reason": expected_reason}
                        )
                        return message_id
                    if kind in {
                        "embedding_message_failed",
                        "embedding_message_input_invalid",
                    }:
                        raise AssertionError("embedding_message_failed")
                following = page.get("nextToken")
                if not following or following == token:
                    break
                token = following
            self.pause()
