#!/usr/bin/env python3
"""ローカルの接続設定からproviderとbackendの入力を同じアカウントで生成する。"""

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"


def main() -> None:
    config = json.loads((LOCAL / "account.json").read_text())
    expected_keys = {
        "expected_account_id",
        "aws_profile",
        "smoke_aws_profile",
        "trusted_admin_role_arn_pattern",
    }
    if not isinstance(config, dict) or set(config) != expected_keys:
        raise ValueError("account.jsonにはサンプルと同じ4項目だけを指定してください。")
    if not all(isinstance(value, str) for value in config.values()):
        raise ValueError("接続設定はすべて文字列で指定してください。")
    account = config["expected_account_id"]
    if not re.fullmatch(r"[0-9]{12}", account):
        raise ValueError("expected_account_idには確認済みの12桁のIDが必要です。")
    for key in ("aws_profile", "smoke_aws_profile"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", config[key]):
            raise ValueError(f"{key}にはAWS CLIのプロファイル名を指定してください。")
    trust_pattern = (
        rf"arn:aws:iam::{account}:role/aws-reserved/sso\.amazonaws\.com/"
        r"(?:ap-northeast-1/)?AWSReservedSSO_[A-Za-z0-9_+=,.@-]+_\*"
    )
    if not re.fullmatch(trust_pattern, config["trusted_admin_role_arn_pattern"]):
        raise ValueError(
            "信頼先には同じアカウントのSSO権限セットを1つ指定してください。"
        )

    bootstrap = {
        key: value for key, value in config.items() if key != "smoke_aws_profile"
    }
    smoke = {"expected_account_id": account, "aws_profile": config["smoke_aws_profile"]}
    backend = {
        "bucket": f"vector-test-tfstate-{account}",
        "profile": config["smoke_aws_profile"],
        "allowed_account_ids": [account],
        "assume_role": {
            "role_arn": (
                f"arn:aws:iam::{account}:role/vector-test/bootstrap/vector-test-terraform"
            ),
            "duration": "1h",
        },
    }
    files = {
        "bootstrap.tfvars.json": json.dumps(bootstrap, indent=2) + "\n",
        "smoke.tfvars.json": json.dumps(smoke, indent=2) + "\n",
        "smoke.tfbackend": "\n".join(
            f"{key} = {json.dumps(value)}" for key, value in backend.items()
        )
        + "\n",
    }
    # 出力先を固定し、秘密値や任意ファイルを書き込む用途には使わない。
    os.umask(0o077)
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    LOCAL.chmod(0o700)
    for name, content in files.items():
        (LOCAL / name).write_text(content)
        (LOCAL / name).chmod(0o600)
    print(
        ".local/にTerraform入力とbackend設定を生成しました。AWSへの接続は行っていません。"
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
