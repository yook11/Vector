import socket
import socketserver
import subprocess
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import probe
from tunnel import HOST, connect_proxy


class EchoHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.accepted += 1
        value = self.request.recv(4096)
        self.request.sendall(value)


class TunnelTests(unittest.TestCase):
    def setUp(self):
        self.upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), EchoHandler)
        self.upstream.accepted = 0
        self.worker = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.close_upstream)

    def close_upstream(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        self.worker.join(timeout=5)

    def connect(self, url):
        parsed = urlsplit(url)
        return socket.create_connection((parsed.hostname, parsed.port), timeout=3)

    def test_only_sqs_connect_passes_bytes_unchanged(self):
        payload = b"\x16\x03\x01\x00\x04\x00\xff\x13\x37"
        with connect_proxy(self.upstream.server_address[1]) as url:
            with self.connect(url) as client:
                client.sendall(
                    f"CONNECT {HOST}:443 HTTP/1.1\r\nHost: {HOST}:443\r\n\r\n".encode()
                )
                self.assertEqual(
                    client.recv(4096), b"HTTP/1.1 200 Connection Established\r\n\r\n"
                )
                client.sendall(payload)
                self.assertEqual(client.recv(4096), payload)
        self.assertEqual(self.upstream.accepted, 1)

    def test_other_destination_is_denied_without_upstream_connection(self):
        with connect_proxy(self.upstream.server_address[1]) as url:
            with self.connect(url) as client:
                client.sendall(b"CONNECT 169.254.169.254:80 HTTP/1.1\r\n\r\n")
                self.assertIn(b"403", client.recv(4096))
        self.assertEqual(self.upstream.accepted, 0)

    def test_other_method_is_denied_without_upstream_connection(self):
        with connect_proxy(self.upstream.server_address[1]) as url:
            with self.connect(url) as client:
                client.sendall(f"GET https://{HOST}/ HTTP/1.1\r\n\r\n".encode())
                self.assertIn(b"403", client.recv(4096))
        self.assertEqual(self.upstream.accepted, 0)

    def test_large_headers_are_denied_without_upstream_connection(self):
        with connect_proxy(self.upstream.server_address[1]) as url:
            with self.connect(url) as client:
                client.sendall(
                    f"CONNECT {HOST}:443 HTTP/1.1\r\nX: ".encode() + b"x" * 4096
                )
                self.assertIn(b"431", client.recv(4096))
        self.assertEqual(self.upstream.accepted, 0)

    def test_sqs_proxy_cannot_be_bypassed_by_inherited_no_proxy(self):
        completed = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with (
            patch.dict(
                probe.os.environ,
                {"https_proxy": "http://untrusted:80", "no_proxy": "*"},
            ),
            patch.object(probe.subprocess, "run", return_value=completed) as run,
        ):
            probe.aws(
                "test",
                "sqs",
                "get-queue-attributes",
                proxy_url="http://127.0.0.1:18444",
            )
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:18444")
        self.assertEqual(env["NO_PROXY"], "")
        self.assertNotIn("https_proxy", env)
        self.assertNotIn("no_proxy", env)
        self.assertNotIn("--no-verify-ssl", run.call_args.args[0])

    def test_credentials_calls_cannot_use_sqs_proxy(self):
        with patch.object(probe.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                probe.aws(
                    "test",
                    "sts",
                    "get-caller-identity",
                    proxy_url="http://127.0.0.1:18444",
                )
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
