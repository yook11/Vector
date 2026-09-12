"""実行ごとに固定する設定、期限付きコマンド、AWS認証。"""

import json
import os
import signal
import subprocess
import time
from contextlib import closing, suppress
from datetime import datetime
from pathlib import Path

from botocore.config import Config
from botocore.session import Session

ROOT = Path(__file__).resolve().parents[4]
LOCAL = ROOT / "infra/aws-test/.local"
REGION = "ap-northeast-1"


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")

    def encode(item):
        if isinstance(item, datetime):
            return item.isoformat()
        raise TypeError(type(item).__name__)

    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=encode) + "\n"
    )
    temporary.chmod(0o600)
    temporary.replace(path)


def execute(args, *, cwd, log, timeout=300, env=None):
    with Path(log).open("w") as output:
        process = subprocess.Popen(  # noqa: S603
            args,
            cwd=cwd,
            stdout=output,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
    if code:
        raise RuntimeError(f"command_failed:{Path(log).name}:{code}")


def session(profile, config_path=None):
    result = Session(profile=profile)
    if config_path:
        result.set_config_variable("config_file", str(config_path))
        result.set_config_variable("credentials_file", "/dev/null")
    return result


def client(aws, service):
    """withに対応しないbotocoreクライアントにも終了時のcloseを保証する。"""
    return closing(
        aws.create_client(
            service,
            region_name=REGION,
            config=Config(
                connect_timeout=5,
                read_timeout=10,
                retries={"total_max_attempts": 2},
                ignore_configured_endpoint_urls=True,
            ),
        )
    )


def identity(aws, account, role):
    with client(aws, "sts") as sts:
        value = sts.get_caller_identity()
    if value["Account"] != account or f":assumed-role/{role}" not in value["Arn"]:
        raise RuntimeError("aws_identity_mismatch")


def wait_command(aws, instance, command_id, deadline):
    with client(aws, "ssm") as ssm:
        while time.monotonic() < deadline:
            try:
                result = ssm.get_command_invocation(
                    CommandId=command_id, InstanceId=instance
                )
            except ssm.exceptions.InvocationDoesNotExist:
                result = {"Status": "Pending"}
            if result["Status"] == "Success" and result["ResponseCode"] == 0:
                return result["StandardOutputContent"]
            if result["Status"] not in {"Pending", "InProgress", "Delayed"}:
                raise RuntimeError(
                    f"ssm_command_failed:{command_id}:{result['Status']}"
                )
            time.sleep(2)
    raise TimeoutError(f"ssm_command_timeout:{command_id}")


def send_command(aws, outputs, command, timeout, journal):
    execution = outputs["execution"]
    with client(aws, "ssm") as ssm:
        result = ssm.send_command(
            InstanceIds=[execution["instance_ids"]["runner"]],
            DocumentName="AWS-RunShellScript",
            TimeoutSeconds=60,
            Parameters={"commands": [command], "executionTimeout": [str(timeout)]},
            CloudWatchOutputConfig={
                "CloudWatchOutputEnabled": True,
                "CloudWatchLogGroupName": execution["log_groups"]["runner"],
            },
        )
    command_id = result["Command"]["CommandId"]
    journal(command_id)
    return wait_command(
        aws,
        execution["instance_ids"]["runner"],
        command_id,
        time.monotonic() + timeout + 90,
    )
