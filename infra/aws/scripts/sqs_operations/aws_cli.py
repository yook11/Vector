"""AWS CLIの資格情報・宛先・エラー出力を制限する。"""

import json
import os
import re
import subprocess

REGION = "ap-northeast-1"
HOST = f"sqs.{REGION}.amazonaws.com"


class AwsFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"AWS操作に失敗しました ({code})。")


def clean_environment():
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AWS_") and not key.lower().endswith("_proxy")
    }
    env.update(
        {
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_PAGER": "",
            "AWS_CLI_AUTO_PROMPT": "off",
            "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "true",
            "AWS_MAX_ATTEMPTS": "1",
            "AWS_RETRY_MODE": "standard",
        }
    )
    return env


def run_json(command, env):
    try:
        result = subprocess.run(
            command, env=env, capture_output=True, text=True, timeout=45
        )
    except subprocess.TimeoutExpired:
        raise AwsFailure("Timeout") from None
    except OSError:
        raise AwsFailure("CliUnavailable") from None
    if result.returncode:
        match = re.search(r"An error occurred \(([A-Za-z0-9_.-]+)\)", result.stderr)
        raise AwsFailure(match.group(1) if match else "CLIError")
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        raise AwsFailure("InvalidResponse") from None


def profile_credentials(profile):
    result = run_json(
        [
            "aws",
            "configure",
            "export-credentials",
            "--profile",
            profile,
            "--format",
            "process",
        ],
        clean_environment(),
    )
    if not all(
        isinstance(result.get(key), str) and result[key]
        for key in (
            "AccessKeyId",
            "SecretAccessKey",
            "SessionToken",
        )
    ):
        raise RuntimeError(
            "運用ロールの一時資格情報を取得できません。SSOログインとprofileを確認してください。"
        )
    return result


class AwsCli:
    def __init__(self, credentials):
        self.credentials = credentials

    def environment(self, proxy_url=None):
        env = clean_environment()
        env.update(
            {
                "AWS_ACCESS_KEY_ID": self.credentials["AccessKeyId"],
                "AWS_SECRET_ACCESS_KEY": self.credentials["SecretAccessKey"],
                "AWS_SESSION_TOKEN": self.credentials["SessionToken"],
            }
        )
        if proxy_url is not None:
            if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", proxy_url):
                raise ValueError("proxyはloopback宛てだけを許可します。")
            env.update({"HTTPS_PROXY": proxy_url, "NO_PROXY": ""})
        return env

    def command(self, service, operation, **arguments):
        if service not in {"sqs", "ssm", "sts", "ec2"}:
            raise ValueError("未対応のAWSサービスです。")
        command = [
            "aws",
            "--region",
            REGION,
            "--output",
            "json",
            "--no-cli-pager",
            "--cli-connect-timeout",
            "10",
            "--cli-read-timeout",
            "30",
            "--endpoint-url",
            f"https://{service}.{REGION}.amazonaws.com",
            service,
            operation,
        ]
        for key, value in arguments.items():
            values = value if isinstance(value, list) else [value]
            command += ["--" + key.replace("_", "-")] + [str(item) for item in values]
        return command

    def request(self, service, operation, *, proxy_url=None, **arguments):
        if proxy_url is not None and service != "sqs":
            raise ValueError("proxyはSQS通信だけに使用します。")
        return run_json(
            self.command(service, operation, **arguments), self.environment(proxy_url)
        )
