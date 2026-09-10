"""読み取り専用の共通イメージ内でRICから実handlerへの接続を確認する。"""

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SITE_CUSTOMIZE = """
import json
from contextlib import asynccontextmanager
from importlib import import_module
from pydantic import SecretStr
from app.lambda_handlers.embedding.resources import EmbeddingResources

entry = import_module("app.lambda_handlers.embedding.handler")
@asynccontextmanager
async def resources(settings):
    print(json.dumps({"probe": "resources_open"}), flush=True)
    try:
        yield EmbeddingResources(
            gemini_api_key=SecretStr("runtime-test-key"), session_factory=object()
        )
    finally:
        print(json.dumps({"probe": "resources_closed"}), flush=True)
entry.open_embedding_resources = resources
"""


def main() -> None:
    results = queue.Queue()
    stopped = threading.Event()

    class RuntimeApi(BaseHTTPRequestHandler):
        invocations = 0

        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            if self.path != "/2018-06-01/runtime/invocation/next":
                self.send_error(404)
                return
            if self.invocations >= 2:
                stopped.wait(30)
                return
            type(self).invocations += 1
            body = b'{"Records": []}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Lambda-Runtime-Aws-Request-Id", f"test-{self.invocations}"
            )
            self.send_header(
                "Lambda-Runtime-Deadline-Ms", str(int(time.time() * 1000) + 120000)
            )
            self.send_header(
                "Lambda-Runtime-Invoked-Function-Arn",
                "arn:aws:lambda:ap-northeast-1:123456789012:function:test",
            )
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            results.put((self.path, json.loads(body)))
            self.send_response(202)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeApi)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory(
        prefix="embedding-runtime-", dir="/tmp"
    ) as directory:  # noqa: S108
        Path(directory, "sitecustomize.py").write_text(SITE_CUSTOMIZE)
        process = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-m",
                "awslambdaric",
                "app.lambda_handlers.embedding.handler",
            ],
            cwd="/app",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={
                "PATH": os.defpath,
                "PYTHONPATH": f"{directory}:/app",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
                "AWS_LAMBDA_RUNTIME_API": f"127.0.0.1:{server.server_port}",
                "AWS_REGION": "ap-northeast-1",
                "ENV": "production",
                "DATABASE_URL": "postgresql+asyncpg://vector_app@db.vector.internal/vector?sslmode=require",
                "DB_IAM_AUTH": "true",
                "GEMINI_API_KEY_PARAMETER_PATH": "/test/consumer/gemini-key",
                "EGRESS_PROXY_URL": "http://proxy.vector.internal:3128",
            },
        )
        received = []
        try:
            for _ in range(2):
                received.append(results.get(timeout=30))
        finally:
            process.terminate()
            stdout, stderr = process.communicate(timeout=10)
            stopped.set()
            server.shutdown()
            server.server_close()
        assert received == [  # noqa: S101
            (
                f"/2018-06-01/runtime/invocation/test-{i}/response",
                {"batchItemFailures": []},
            )
            for i in (1, 2)
        ], (received, stderr)
        logs = [
            json.loads(line) for line in stdout.splitlines() if line.startswith("{")
        ]
        assert [record["probe"] for record in logs if "probe" in record] == [  # noqa: S101
            "resources_open",
            "resources_closed",
            "resources_open",
            "resources_closed",
        ], (logs, stderr)
    print(
        "PASS: ARM64 RIC -> handler, two invocations, resource cleanup, read-only root"
    )


if __name__ == "__main__":
    main()
