"""外部・内部のclientが、TLSの接続相手をcertifiの一覧で検証することを確かめる。"""

import ssl
from pathlib import Path
from unittest.mock import AsyncMock

import certifi
import httpcore2
import pytest
import truststore

from app.http.external import make_external_async_client
from app.http.internal import make_internal_async_client
from tests.test_http._proxy_exchange import PROXY_URL, PUBLIC_ADDRESS

_OK = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"


class _TlsRecordingStream(httpcore2.AsyncMockStream):
    """TLSの開始に渡された相手のホスト名とcontextを記録する模擬接続。"""

    def __init__(self, buffer: list[bytes]) -> None:
        super().__init__(buffer)
        self.tls_handshakes: list[tuple[str | None, ssl.SSLContext]] = []

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore2.AsyncNetworkStream:
        self.tls_handshakes.append((server_hostname, ssl_context))
        return await super().start_tls(ssl_context, server_hostname, timeout)


def _install_network(
    monkeypatch: pytest.MonkeyPatch, *responses: bytes
) -> _TlsRecordingStream:
    stream = _TlsRecordingStream(list(responses))
    monkeypatch.setattr(
        "httpcore2.AnyIOBackend.connect_tcp", AsyncMock(return_value=stream)
    )
    return stream


def _assert_verifies_with_certifi(context: ssl.SSLContext) -> None:
    certifi_bundle = Path(certifi.where()).read_text()
    assert not isinstance(context, truststore.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert context.cert_store_stats()["x509_ca"] == certifi_bundle.count(
        "-----BEGIN CERTIFICATE-----"
    )


async def test_external_client_verifies_destination_with_certifi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外部宛のTLSは、呼び出し側のverify指定やhttpx2の既定によらずcertifiで検証する。"""
    monkeypatch.setenv("EGRESS_PROXY_URL", PROXY_URL)
    monkeypatch.setattr(
        "app.http.destination_resolution._resolve_host",
        AsyncMock(return_value=[PUBLIC_ADDRESS]),
    )
    stream = _install_network(
        monkeypatch, b"HTTP/1.1 200 Connection established\r\n\r\n", _OK
    )

    async with make_external_async_client(verify=False) as client:
        response = await client.get("https://articles.example/news")

    assert response.status_code == 200
    [(_, context)] = stream.tls_handshakes
    _assert_verifies_with_certifi(context)


async def test_https_proxy_connection_is_verified_with_certifi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """プロキシがhttpsなら、プロキシとのTLSも接続先と同じcertifiの一覧で検証する。"""
    monkeypatch.setenv("EGRESS_PROXY_URL", "https://proxy.vector.internal:3128")
    monkeypatch.setattr(
        "app.http.destination_resolution._resolve_host",
        AsyncMock(return_value=[PUBLIC_ADDRESS]),
    )
    stream = _install_network(
        monkeypatch, b"HTTP/1.1 200 Connection established\r\n\r\n", _OK
    )

    async with make_external_async_client() as client:
        response = await client.get("https://articles.example/news")

    assert response.status_code == 200
    assert [hostname for hostname, _ in stream.tls_handshakes] == [
        "proxy.vector.internal",
        "articles.example",
    ]
    for _, context in stream.tls_handshakes:
        _assert_verifies_with_certifi(context)


async def test_internal_client_verifies_destination_with_certifi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内部宛のTLSも、外部宛と同じcertifiの一覧で検証する。"""
    stream = _install_network(monkeypatch, _OK)

    async with make_internal_async_client(timeout=5) as client:
        response = await client.get("https://gateway.internal.example/mcp")

    assert response.status_code == 200
    [(_, context)] = stream.tls_handshakes
    _assert_verifies_with_certifi(context)
