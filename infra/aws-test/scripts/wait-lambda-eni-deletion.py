#!/usr/bin/env python3
"""LambdaがENIを削除するまで待ち、確認不能ならTerraformの削除を止める。"""

import json
import os
import re
import subprocess
import time

WAIT_SECONDS = 3000


def main() -> None:
    config = json.loads(os.environ["VECTOR_ENI_CLEANUP"])
    patterns = {
        "expected_account_id": r"[0-9]{12}",
        "aws_profile": r"[A-Za-z0-9][A-Za-z0-9_.-]*",
        "region": r"ap-northeast-1",
        "subnet_id": r"subnet-[a-f0-9]+",
        "security_group_id": r"sg-[a-f0-9]+",
    }
    if not isinstance(config, dict) or any(
        not isinstance(config.get(key), str) or not re.fullmatch(pattern, config[key])
        for key, pattern in patterns.items()
    ):
        raise ValueError("ENI削除待機の対象情報が不正です。")

    def read_aws(*arguments: str) -> dict:
        response = subprocess.run(
            [
                "aws",
                *arguments,
                "--profile",
                config["aws_profile"],
                "--region",
                config["region"],
                "--output",
                "json",
                "--no-cli-pager",
                "--cli-connect-timeout",
                "5",
                "--cli-read-timeout",
                "10",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        value = json.loads(response.stdout)
        if not isinstance(value, dict):
            raise ValueError("AWSの応答形式を確認できませんでした。")
        return value

    if (
        read_aws("sts", "get-caller-identity").get("Account")
        != config["expected_account_id"]
    ):
        raise ValueError("接続先アカウントが一致しないため削除待機を停止しました。")

    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        result = read_aws(
            "ec2",
            "describe-network-interfaces",
            "--filters",
            f"Name=subnet-id,Values={config['subnet_id']}",
            f"Name=group-id,Values={config['security_group_id']}",
        )
        interfaces = result.get("NetworkInterfaces")
        if not isinstance(interfaces, list):
            raise ValueError("ENIの一覧を確認できませんでした。")
        if not interfaces:
            print("Lambda用サブネット・SGにENIが残っていないことを確認しました。")
            return
        print(f"ENI消滅待機中: {len(interfaces)}件", flush=True)
        time.sleep(min(10, max(0, deadline - time.monotonic())))
    raise TimeoutError(
        "ENIが50分以内に消滅しませんでした。stateと権限を保持して再試行してください。"
    )


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
        if isinstance(error, subprocess.SubprocessError):
            raise SystemExit(
                "AWS読取に失敗しました。SSO認証・権限・通信を確認して再試行してください。"
            ) from None
        raise SystemExit(str(error)) from None
