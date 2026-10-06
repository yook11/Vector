"""PC上のTLS通信を固定宛先のSSMトンネルへ中継する。"""

from contextlib import contextmanager
import os
import re
import select
import signal
import socket
import socketserver
import subprocess
import threading
import time
import sys

from .aws_cli import HOST


class ConnectHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        header = bytearray()
        try:
            while not header.endswith(b"\r\n\r\n"):
                if len(header) >= 4096:
                    self.request.sendall(
                        b"HTTP/1.1 431 Request Header Fields Too Large\r\n\r\n"
                    )
                    return
                value = self.request.recv(1)
                if not value:
                    return
                header.extend(value)
            if bytes(header).split(b"\r\n", 1)[0] not in {
                f"CONNECT {HOST}:443 HTTP/1.0".encode(),
                f"CONNECT {HOST}:443 HTTP/1.1".encode(),
            }:
                self.request.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            with socket.create_connection(
                ("127.0.0.1", self.server.tunnel_port), timeout=10
            ) as upstream:
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.request.settimeout(30)
                upstream.settimeout(30)
                while True:
                    readable, _, _ = select.select([self.request, upstream], [], [], 30)
                    if not readable:
                        return
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        destination = (
                            upstream if source is self.request else self.request
                        )
                        destination.sendall(data)
        except OSError:
            return


class ConnectServer(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self, tunnel_port):
        self.tunnel_port = tunnel_port
        super().__init__(("127.0.0.1", 0), ConnectHandler)


@contextmanager
def connect_proxy(tunnel_port):
    with ConnectServer(tunnel_port) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            worker.join(timeout=5)


@contextmanager
def sqs_tunnel(client, instance_id, document_name):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    process = subprocess.Popen(
        client.command(
            "ssm",
            "start-session",
            target=instance_id,
            document_name=document_name,
            parameters=f'{{"localPortNumber":["{port}"]}}',
        ),
        env=client.environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    session_ids = []
    ready = threading.Event()

    def read_status():
        for line in process.stdout:
            match = re.search(
                r"Starting session with SessionId: ([A-Za-z0-9_:.@-]+)", line
            )
            if match:
                session_ids.append(match.group(1))
            if "Waiting for connections" in line:
                ready.set()

    reader = threading.Thread(target=read_status, daemon=True)
    reader.start()
    cleanup_failed = False
    try:
        deadline = time.monotonic() + 60
        while not ready.wait(0.25):
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError(
                    "SSMトンネルを開始できません。接続権限・SSM登録・pluginを確認してください。"
                )
        with connect_proxy(port) as proxy_url:
            yield proxy_url
    finally:
        try:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
        except ProcessLookupError:
            pass
        finally:
            reader.join(timeout=5)
            process.stdout.close()
            for session_id in session_ids:
                try:
                    client.request("ssm", "terminate-session", session_id=session_id)
                except (RuntimeError, OSError):
                    cleanup_failed = True
                    print(
                        f"SSMセッションの終了確認が必要です: {session_id}",
                        file=sys.stderr,
                    )
            if not session_ids:
                print(
                    "SSM開始結果を取得できませんでした。管理者で残存セッションを確認してください。",
                    file=sys.stderr,
                )
            if cleanup_failed and sys.exc_info()[0] is None:
                raise RuntimeError("SSMセッションの終了を確認できませんでした。")
