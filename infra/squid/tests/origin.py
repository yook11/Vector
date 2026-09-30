"""試験宛先のTCP接続数と受け取った要求を、別の管理ポートで観測する。"""

import json
import socket
import ssl
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread


class ObservedOrigin(HTTPServer):
    address_family = socket.AF_INET6

    def __init__(self, port: int, tls: ssl.SSLContext | None = None) -> None:
        self.connections = 0
        self.requests: list[dict[str, str]] = []
        self.tls = tls
        super().__init__(("::", port), OriginHandler)

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()

    def get_request(self):
        connection, address = super().get_request()
        self.connections += 1
        if self.tls is not None:
            connection.settimeout(5)
            connection = self.tls.wrap_socket(connection, server_side=True)
        return connection, address


class OriginHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.server.requests.append({"host": self.headers["Host"], "path": self.path})
        body = b"squid-test-origin"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


class ObservationHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        origin = origins[int(self.path.removeprefix("/"))]
        body = json.dumps(
            {"connections": origin.connections, "requests": origin.requests}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


if __name__ == "__main__":
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain("/tls/cert.pem", "/tls/key.pem")
    origins = {
        80: ObservedOrigin(80),
        443: ObservedOrigin(443, tls),
        8080: ObservedOrigin(8080),
    }
    for origin in origins.values():
        Thread(target=origin.serve_forever, daemon=True).start()
    HTTPServer(("127.0.0.1", 8081), ObservationHandler).serve_forever()
