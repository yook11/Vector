"""起動確認済みのEC2で、検証対象イメージを取得してDBを初期化する。"""

import base64
import io
import json
import shlex
import tarfile
from pathlib import Path

from .common import REGION, send_command


def ensure_image(aws, outputs, journal):
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
if ! docker image inspect {shlex.quote(image)} >/dev/null 2>&1; then
  aws ecr get-login-password --region {REGION} | \\
    docker login --username AWS --password-stdin {shlex.quote(registry)} >/dev/null
  docker pull {shlex.quote(image)} >/var/lib/vector-test/backend-image-pull.log 2>&1
fi
docker image inspect --format '{{{{json .RepoDigests}}}}' {shlex.quote(image)}
"""
    result = send_command(aws, outputs, "bash -c " + shlex.quote(command), 300, journal)
    digests = json.loads(result.strip().splitlines()[-1])
    if not isinstance(digests, list) or image not in digests:
        raise RuntimeError("backend_image_digest_unconfirmed")
    return {"status": "available", "image": image}


def prepare(aws, outputs, directory, journal):
    image = outputs["run"]["backend_image"]
    assets = directory / "prepared-assets"
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        for source, name in [
            (assets / "auth.sql", "auth.sql"),
            (assets / "source/infra/aws/db-provision.sql", "db-provision.sql"),
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
code=0
timeout --signal=TERM --kill-after=10 650 docker run --rm --pull=never --network host \\
  --env-file /etc/vector-test/proxy.env --env PYTHONPATH=/app \\
  --env ALEMBIC_ALLOW_DESTRUCTIVE=yes-i-know \\
  --mount "type=bind,source=$work,target=/prepare,readonly" \\
  --entrypoint /app/.venv/bin/python {shlex.quote(image)} \\
  /prepare/prepare_database.py {settings} >"$work/result.log" 2>&1 || code=$?
result=$(tail -n 1 "$work/result.log")
case "$result" in
  '{{"status": '*) printf '%s\\n' "$result" ;;
  *) printf '%s\\n' '{{"status":"failed","reason":"database_process_failed"}}' ;;
esac
printf '{{"exit_code":%s}}\\n' "$code"
"""
    result = send_command(aws, outputs, command, 660, journal)
    # migrationの通常出力に続く最終JSONだけを準備結果として記録する。
    lines = result.strip().splitlines()
    final, process = (json.loads(line) for line in lines[-2:])
    if final.get("status") == "failed":
        reasons = {
            "database_process_failed",
            "database_prepare_in_progress",
            "database_incomplete_destroy_then_use_new_run_id",
            "database_revision_mismatch_destroy_then_use_new_run_id",
            "database_required_state_missing_destroy_then_use_new_run_id",
            "database_connection_failed",
            "database_operation_failed",
            "database_prepare_timeout",
        }
        reason = final.get("reason")
        raise RuntimeError(
            reason if reason in reasons else "database_preparation_failed"
        )
    if (
        process.get("exit_code") != 0
        or final.get("status") != "prepared"
        or not final.get("heads")
        or final.get("verified") is not True
        or final.get("action") not in {"initialized", "verified"}
    ):
        raise RuntimeError("database_preparation_unconfirmed")
    return final
