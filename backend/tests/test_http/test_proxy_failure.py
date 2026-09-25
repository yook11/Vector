"""接続先だけをモックし、HTTPX・httpcoreを通したプロキシ失敗の観測範囲を検証する。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest

from app.http.error_mapping import (
    http_response_error_from_exception,
    http_transport_error_from_exception,
)
from app.http.failure import (
    HttpTransportFailure,
)
from app.http.failure import (
    HttpTransportFailureReason as Reason,
)
from app.http.failure import (
    HttpTransportStage as Stage,
)

_PROXY = "http://proxy.vector.internal:3128"
_RECEIVED_AT = datetime(2026, 9, 23, tzinfo=UTC)


def _install_proxy_stream(monkeypatch: pytest.MonkeyPatch, *responses: bytes) -> None:
    """HTTPのパースと例外変換を実装に任せ、ネットワークの読み書きだけ差し替える。"""
    monkeypatch.setattr(
        "httpcore.AnyIOBackend.connect_tcp",
        AsyncMock(return_value=httpcore.AsyncMockStream(list(responses))),
    )


async def test_connect_forbidden_preserves_proxy_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONNECTの403を取得先応答へ読み替えず、プロキシ接続時の事実として保持する。"""
    _install_proxy_stream(
        monkeypatch, b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n"
    )
    async with httpx.AsyncClient(proxy=_PROXY, trust_env=False) as client:
        with pytest.raises(httpx.ProxyError) as caught:
            await client.get("https://example.invalid/article")

    mapped = http_transport_error_from_exception(caught.value)
    assert mapped is not None
    assert mapped.failure == HttpTransportFailure(
        Stage.CONNECT, Reason.PROXY, proxy_status=403
    )


async def test_connect_unavailable_preserves_proxy_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONNECTの503も、終了・再試行を決めずにプロキシ接続時の事実として保持する。"""
    _install_proxy_stream(
        monkeypatch,
        b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n",
    )
    async with httpx.AsyncClient(proxy=_PROXY, trust_env=False) as client:
        with pytest.raises(httpx.ProxyError) as caught:
            await client.get("https://example.invalid/article")

    mapped = http_transport_error_from_exception(caught.value)
    assert mapped is not None
    assert mapped.failure == HttpTransportFailure(
        Stage.CONNECT, Reason.PROXY, proxy_status=503
    )


async def test_forwarded_forbidden_response_is_not_assumed_proxy_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP転送の403は、ヘッダーだけから生成元や拒否したACLを推測しない。"""
    _install_proxy_stream(
        monkeypatch,
        b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n"
        b"X-Squid-Error: ERR_ACCESS_DENIED 0\r\n\r\n",
    )
    async with httpx.AsyncClient(proxy=_PROXY, trust_env=False) as client:
        response = await client.get("http://example.invalid/article")
        with pytest.raises(httpx.HTTPStatusError) as caught:
            response.raise_for_status()

    assert http_transport_error_from_exception(caught.value) is None
    mapped = http_response_error_from_exception(caught.value, received_at=_RECEIVED_AT)
    assert mapped.status_code == 403


async def test_forbidden_response_after_tunnel_is_not_proxy_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONNECT成功後の403は、CONNECT自体の拒否とは区別する。"""
    _install_proxy_stream(
        monkeypatch,
        b"HTTP/1.1 200 Connection Established\r\n\r\n",
        b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n",
    )
    async with httpx.AsyncClient(proxy=_PROXY, trust_env=False) as client:
        response = await client.get("https://example.invalid/article")
        with pytest.raises(httpx.HTTPStatusError) as caught:
            response.raise_for_status()

    assert http_transport_error_from_exception(caught.value) is None
    mapped = http_response_error_from_exception(caught.value, received_at=_RECEIVED_AT)
    assert mapped.status_code == 403


async def test_proxy_tcp_failure_has_no_refusal_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """プロキシのTCP接続失敗から、拒否応答や分類器に渡されていない経路情報を捏造しない。"""
    monkeypatch.setattr(
        "anyio.connect_tcp", AsyncMock(side_effect=ConnectionRefusedError("refused"))
    )
    async with httpx.AsyncClient(proxy=_PROXY, trust_env=False) as client:
        with pytest.raises(httpx.ConnectError) as caught:
            await client.get("https://example.invalid/article")

    mapped = http_transport_error_from_exception(caught.value)
    assert mapped is not None
    assert mapped.failure == HttpTransportFailure(Stage.CONNECT, Reason.NETWORK_IO)
