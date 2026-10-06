import socket
import socketserver
from pathlib import Path
import sys
import threading
import unittest
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqs_operations.tunnel import HOST, connect_proxy  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
