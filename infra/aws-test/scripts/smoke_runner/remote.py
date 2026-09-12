"""実行用EC2の準備完了を待ち、検証対象イメージでDBを初期化する。"""

import base64
import io
import json
import shlex
import tarfile
import time
from pathlib import Path

from .common import REGION, client, send_command


def prepare(aws, outputs, directory, journal):
    instance = outputs["execution"]["instance_ids"]["runner"]
    deadline = time.monotonic() + 900
    with client(aws, "ssm") as ssm:
        while time.monotonic() < deadline:
            entries = ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [instance]}]
            )["InstanceInformationList"]
            if entries and entries[0]["PingStatus"] == "Online":
                break
            time.sleep(5)
        else:
            raise TimeoutError("runner_ssm_not_ready")
    command = """set -eu
while ! test -f /var/lib/vector-test/bootstrap-status.json; do sleep 2; done
while true; do
  status=$(cat /var/lib/vector-test/bootstrap-status.json)
  case "$status" in
    *'"ready"'*) printf '%s\n' "$status"; exit 0 ;;
    *'"failed"'*) exit 1 ;;
  esac
  sleep 2
done
"""
    send_command(aws, outputs, command, 600, journal)
    image = outputs["run"]["backend_image"]
    registry = image.split("/")[0]
    # Dockerの認証情報はコマンド専用ディレクトリに置き、pull後に削除する。
    command = f"""#!/bin/bash
set -euo pipefail
set -a
. /etc/vector-test/proxy.env
set +a
export DOCKER_CONFIG=$(mktemp -d)
trap 'rm -rf "$DOCKER_CONFIG"' EXIT
aws ecr get-login-password --region {REGION} | \\
  docker login --username AWS --password-stdin {shlex.quote(registry)}
docker pull {shlex.quote(image)}
"""
    send_command(aws, outputs, "bash -c " + shlex.quote(command), 300, journal)
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        for source, name in [
            (directory / "auth.sql", "auth.sql"),
            (directory / "source/infra/aws/db-provision.sql", "db-provision.sql"),
            (Path(__file__).with_name("prepare_database.py"), "prepare_database.py"),
        ]:
            archive.add(source, arcname=name)
    encoded = base64.b64encode(payload.getvalue()).decode()
    if len(encoded) > 40000:
        raise ValueError("preparation_payload_too_large")
    settings = shlex.quote(json.dumps(outputs["database"], separators=(",", ":")))
    command = f"""set -eu
work=$(mktemp -d /var/lib/vector-test/prepare.XXXXXX)
trap 'rm -rf "$work"' EXIT
printf '%s' '{encoded}' | base64 -d | tar xz -C "$work"
chmod 755 "$work"
chmod 644 "$work"/*
timeout --signal=TERM --kill-after=10 650 docker run --rm --pull=never --network host \\
  --env-file /etc/vector-test/proxy.env --env PYTHONPATH=/app \\
  --env ALEMBIC_ALLOW_DESTRUCTIVE=yes-i-know \\
  --mount "type=bind,source=$work,target=/prepare,readonly" \\
  --entrypoint /app/.venv/bin/python {shlex.quote(image)} \\
  /prepare/prepare_database.py {settings}
"""
    result = send_command(aws, outputs, command, 660, journal)
    # migrationの通常出力に続く最終JSONだけを準備結果として記録する。
    final = json.loads(result.strip().splitlines()[-1])
    if final.get("status") != "prepared" or not final.get("heads"):
        raise RuntimeError("database_preparation_unconfirmed")
    return final
