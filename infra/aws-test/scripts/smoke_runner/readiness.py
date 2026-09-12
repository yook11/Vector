"""DB準備に進む前に、管理通信とプロキシ経由の外向き通信を確認する。"""

import json
import shlex
import time
from uuid import uuid4

from .common import REGION, client, send_command

PHASES = ("ec2", "ssm", "ssm_command", "bootstrap", "proxy_tcp", "proxy_ecr")


def check(manager, runner, outputs, phase, journal):
    started = time.monotonic()
    deadline = started + 900
    last_progress = started
    step = "ec2"

    def progress():
        nonlocal last_progress
        current = time.monotonic()
        if current >= deadline:
            raise TimeoutError(f"readiness_timeout:{step}")
        if current - last_progress >= 30:
            print(f"{step}: 起動確認中 {int(current - started)}秒", flush=True)
            last_progress = current

    def wait(predicate):
        while True:
            progress()
            ready = predicate()
            progress()
            if ready:
                return
            time.sleep(min(5, max(0, deadline - time.monotonic())))

    def command(script):
        progress()
        result = send_command(
            runner,
            outputs,
            "bash -c " + shlex.quote(script),
            30,
            journal,
            cloudwatch=False,
            deadline=deadline,
            progress=progress,
        )
        progress()
        return result.strip()

    ids = list(outputs["execution"]["instance_ids"].values())
    with phase(step), client(manager, "ec2") as ec2:

        def ec2_ready():
            items = ec2.describe_instance_status(
                InstanceIds=ids,
                IncludeAllInstances=True,
            )["InstanceStatuses"]
            if any(
                i["InstanceState"]["Name"]
                in {
                    "stopping",
                    "stopped",
                    "shutting-down",
                    "terminated",
                }
                for i in items
            ):
                raise RuntimeError("runtime_instance_not_running")
            return {i["InstanceId"] for i in items} == set(ids) and all(
                i["InstanceState"]["Name"] == "running"
                and i["InstanceStatus"]["Status"] == "ok"
                and i["SystemStatus"]["Status"] == "ok"
                for i in items
            )

        wait(ec2_ready)

    step = "ssm"
    with phase(step), client(runner, "ssm") as ssm:

        def ssm_ready():
            items = ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": ids}],
            )["InstanceInformationList"]
            return {i["InstanceId"] for i in items} == set(ids) and all(
                i["PingStatus"] == "Online" for i in items
            )

        wait(ssm_ready)

    step = "ssm_command"
    with phase(step):
        token = "vector-smoke-" + uuid4().hex
        if command("printf '%s\\n' " + shlex.quote(token)) != token:
            raise RuntimeError("ssm_response_mismatch")

    step = "bootstrap"
    with phase(step):

        def bootstrap_ready():
            result = command("""set -eu
if test -f /var/lib/vector-test/bootstrap-status.json; then
  cat /var/lib/vector-test/bootstrap-status.json
else
  printf '%s\n' '{"status":"starting"}'
fi
systemctl is-active docker || true
""")
            lines = result.splitlines()
            if len(lines) != 2:
                raise RuntimeError("bootstrap_response_invalid")
            status = json.loads(lines[0]).get("status")
            if status == "failed":
                raise RuntimeError("runner_bootstrap_failed")
            if status not in {"starting", "ready"}:
                raise RuntimeError("bootstrap_status_invalid")
            if status == "ready" and lines[1] != "active":
                raise RuntimeError("runner_docker_not_active")
            return status == "ready"

        wait(bootstrap_ready)

    step = "proxy_tcp"
    with phase(step):
        # 通信先は保存済みの試験用起動設定と同じ固定アドレスに限定する。
        if (
            command("""set -eu
timeout 5 bash -c 'exec 3<>/dev/tcp/10.80.0.10/3128'
printf '%s\n' proxy_connected
""")
            != "proxy_connected"
        ):
            raise RuntimeError("proxy_connection_unconfirmed")

    step = "proxy_ecr"
    with phase(step):
        run = outputs["run"]
        args = [
            "aws",
            "ecr",
            "batch-get-image",
            "--registry-id",
            run["account_id"],
            "--repository-name",
            "vector-test/proxy",
            "--image-ids",
            "imageDigest=" + run["proxy_image_digest"],
            "--region",
            REGION,
            "--cli-connect-timeout",
            "5",
            "--cli-read-timeout",
            "10",
            "--no-cli-pager",
            "--output",
            "json",
            "--query",
            "{images: images[*].imageId.imageDigest, "
            "failures: failures[*].failureCode}",
        ]
        result = json.loads(
            command(
                "set -euo pipefail\nset -a\n. /etc/vector-test/proxy.env\nset +a\n"
                "export AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true AWS_MAX_ATTEMPTS=1\n"
                + shlex.join(args)
            )
        )
        if result != {"images": [run["proxy_image_digest"]], "failures": []}:
            raise RuntimeError("proxy_ecr_digest_unconfirmed")
