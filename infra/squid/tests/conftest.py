"""製品のSquid設定を、外向き経路のないDockerネットワークで実行する。"""

import json
import subprocess
import time
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[3]
TESTS = Path(__file__).resolve().parent
PYTHON_IMAGE = "python:3.13-alpine"
SUBNET = "93.184.216.0/24"
PROXY_IP = "93.184.216.10"
ORIGIN_IP = "93.184.216.20"
PRIVATE_SUBNET = "10.254.216.0/24"
PRIVATE_ORIGIN_IP = "10.254.216.20"
PRIVATE_IPV6_SUBNET = "fd00:285::/64"
PRIVATE_ORIGIN_IPV6 = "fd00:285::20"


def command(*args: str, input: str | None = None, cwd: Path | None = None) -> str:
    result = subprocess.run(
        args, input=input, cwd=cwd, text=True, capture_output=True, timeout=120
    )
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def render_config(directory: Path) -> str:
    ranges = json.loads((ROOT / "backend/app/http/non_public_ranges.json").read_text())
    variables = {
        "listen_port": 3128,
        "private_v4_ranges": ranges["v4"],
        "private_v6_ranges": ranges["v6"],
        "clients": {
            "test_client": {
                "cidr": f"{ORIGIN_IP}/32",
                "domains": ["allowed.test", "private.test"],
                "allow_any_domain": False,
            },
            "other_source": {
                "cidr": "93.184.216.21/32",
                "domains": ["source-b.test"],
                "allow_any_domain": False,
            },
            "article_fetch": {
                "cidr": "93.184.216.22/32",
                "domains": [],
                "allow_any_domain": True,
            },
        },
    }
    template = str(ROOT / "infra/aws/templates/squid.conf.tftpl")
    expression = (
        f"jsonencode(templatefile({json.dumps(template)}, {json.dumps(variables)}))"
    )
    output = command(
        "terraform", "console", "-no-color", input=expression, cwd=directory
    )
    return json.loads(json.loads(output))


def remove_container(name: str) -> None:
    """pytestが失敗時に示すログを回収し、起動失敗でもコンテナを削除する。"""
    try:
        logs = subprocess.run(
            ["docker", "logs", name], text=True, capture_output=True, timeout=10
        )
        print(f"{name}:\n{logs.stdout}{logs.stderr}")
    finally:
        command("docker", "rm", "--force", name)


@dataclass
class ProxyTrial:
    origin_container: str
    proxy_container: str
    network: str
    tls_directory: Path
    resources: ExitStack
    clients: dict[str, str] = field(default_factory=dict)

    def python(self, source: str) -> str:
        return command(
            "docker", "exec", "-i", self.origin_container, "python", "-", input=source
        )

    def client_container(self, source_ip: str) -> str:
        if source_ip == ORIGIN_IP:
            return self.origin_container
        if source_ip not in self.clients:
            name = f"{self.network}-client-{len(self.clients)}"
            command(
                "docker",
                "create",
                "--name",
                name,
                "--network",
                self.network,
                "--ip",
                source_ip,
                "--dns",
                "127.0.0.1",
                "--mount",
                f"type=bind,source={TESTS},target=/tests,readonly",
                "--mount",
                f"type=bind,source={self.tls_directory},target=/tls,readonly",
                PYTHON_IMAGE,
                "python",
                "-c",
                "import time; time.sleep(3600)",
            )
            self.resources.callback(remove_container, name)
            command("docker", "start", name)
            self.clients[source_ip] = name
        return self.clients[source_ip]

    def request(self, container: str, request: dict) -> dict:
        return json.loads(
            command(
                "docker",
                "exec",
                "-i",
                container,
                "python",
                "/tests/client.py",
                input=json.dumps(request),
            )
        )

    def get(self, url: str, *, source_ip: str = ORIGIN_IP) -> dict:
        return self.request(
            self.client_container(source_ip),
            {"method": "GET", "url": url, "proxy": PROXY_IP},
        )

    def connect(self, authority: str, *, source_ip: str = ORIGIN_IP) -> dict:
        return self.request(
            self.client_container(source_ip),
            {"method": "CONNECT", "authority": authority, "proxy": PROXY_IP},
        )

    def check_destination_reachable(self, url: str) -> None:
        """Squidと同じ経路で事前に接続し、経路不通をACL拒否と誤認しない。"""
        name = f"{self.network}-reachability"
        with ExitStack() as resources:
            command(
                "docker",
                "create",
                "--name",
                name,
                "--interactive",
                "--network",
                f"container:{self.proxy_container}",
                "--mount",
                f"type=bind,source={TESTS},target=/tests,readonly",
                "--mount",
                f"type=bind,source={self.tls_directory},target=/tls,readonly",
                PYTHON_IMAGE,
                "python",
                "/tests/client.py",
            )
            resources.callback(remove_container, name)
            output = command(
                "docker",
                "start",
                "--attach",
                "--interactive",
                name,
                input=json.dumps({"method": "GET", "url": url, "proxy": None}),
            )
            if json.loads(output) != {"status": 200, "body": "squid-test-origin"}:
                raise RuntimeError(f"destination_unreachable: {url}")

    def origin_observation(self, port: int = 80) -> dict:
        return json.loads(
            self.python(
                "import http.client\n"
                "connection = http.client.HTTPConnection(\n"
                "    '127.0.0.1', 8081, timeout=2)\n"
                f"connection.request('GET', '/{port}')\n"
                "print(connection.getresponse().read().decode())\n"
                "connection.close()\n"
            )
        )


@pytest.fixture(scope="session")
def squid_image() -> Iterator[str]:
    command("docker", "image", "inspect", PYTHON_IMAGE)
    image = f"vector-squid-access-test:{uuid4().hex}"
    command("docker", "build", "--tag", image, str(ROOT / "infra/squid"))
    try:
        yield image
    finally:
        command("docker", "image", "rm", image)


@pytest.fixture(scope="session")
def tls_directory(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("squid-tls")
    command(
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "1",
        "-keyout",
        str(directory / "key.pem"),
        "-out",
        str(directory / "cert.pem"),
        "-subj",
        "/CN=allowed.test",
        "-addext",
        "subjectAltName=DNS:allowed.test,DNS:private.test,"
        f"IP:{ORIGIN_IP},IP:{PRIVATE_ORIGIN_IP},IP:{PRIVATE_ORIGIN_IPV6}",
    )
    return directory


@pytest.fixture
def proxy_trial(
    squid_image: str, tmp_path: Path, tls_directory: Path
) -> Iterator[ProxyTrial]:
    prefix = f"vector-squid-test-{uuid4().hex[:12]}"
    origin = f"{prefix}-origin"
    proxy = f"{prefix}-proxy"
    private_network = f"{prefix}-private"
    config = render_config(tmp_path)
    with ExitStack() as resources:
        command("docker", "network", "create", "--internal", "--subnet", SUBNET, prefix)
        resources.callback(command, "docker", "network", "rm", prefix)
        command(
            "docker",
            "network",
            "create",
            "--internal",
            "--ipv6",
            "--subnet",
            PRIVATE_SUBNET,
            "--subnet",
            PRIVATE_IPV6_SUBNET,
            private_network,
        )
        resources.callback(command, "docker", "network", "rm", private_network)
        command(
            "docker",
            "create",
            "--name",
            origin,
            "--network",
            prefix,
            "--ip",
            ORIGIN_IP,
            "--network-alias",
            "allowed.test",
            "--network-alias",
            "blocked.test",
            "--network-alias",
            "unlisted.test",
            "--network-alias",
            "source-b.test",
            "--dns",
            "127.0.0.1",
            "--mount",
            f"type=bind,source={TESTS},target=/tests,readonly",
            "--mount",
            f"type=bind,source={tls_directory},target=/tls,readonly",
            PYTHON_IMAGE,
            "python",
            "/tests/origin.py",
        )
        resources.callback(remove_container, origin)
        command(
            "docker",
            "network",
            "connect",
            "--ip",
            PRIVATE_ORIGIN_IP,
            "--ip6",
            PRIVATE_ORIGIN_IPV6,
            "--alias",
            "private.test",
            private_network,
            origin,
        )
        command("docker", "start", origin)
        command(
            "docker",
            "create",
            "--name",
            proxy,
            "--network",
            prefix,
            "--ip",
            PROXY_IP,
            "--dns",
            "127.0.0.1",
            "--env",
            f"SQUID_CONF={config}",
            squid_image,
        )
        resources.callback(remove_container, proxy)
        command(
            "docker",
            "network",
            "connect",
            "--ip",
            "10.254.216.10",
            "--ip6",
            "fd00:285::10",
            private_network,
            proxy,
        )
        command("docker", "start", proxy)
        trial = ProxyTrial(origin, proxy, prefix, tls_directory, resources)
        deadline = time.monotonic() + 15
        while True:
            try:
                trial.origin_observation()
                trial.python(
                    "import socket\n"
                    "socket.create_connection(\n"
                    f"    ({PROXY_IP!r}, 3128), timeout=1).close()\n"
                )
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        yield trial
