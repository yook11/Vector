"""通信例外の分類、対象外の境界、SDKのラップ経路を検証する。"""

import socket
import ssl
from contextlib import closing
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from botocore import exceptions as botocore_errors
from botocore.awsrequest import AWSPreparedRequest
from botocore.httpsession import URLLib3Session
from urllib3 import exceptions as urllib3_errors

from app.http.destination_policy import HostBlockedError
from app.http.destination_resolution import HostResolutionError
from app.http.failure import (
    HttpTransportFailure,
    HttpTransportFailureReason,
    HttpTransportStage,
    classify_botocore,
    classify_httpx,
)

S = HttpTransportStage
R = HttpTransportFailureReason


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (httpx.PoolTimeout("timeout"), HttpTransportFailure(S.PREPARATION, R.TIMEOUT)),
        (
            HostResolutionError("failed"),
            HttpTransportFailure(S.PREPARATION, R.DNS_RESOLUTION),
        ),
        (httpx.ConnectTimeout("timeout"), HttpTransportFailure(S.CONNECT, R.TIMEOUT)),
        (httpx.ConnectError("failed"), HttpTransportFailure(S.CONNECT, R.NETWORK_IO)),
        (httpx.WriteTimeout("timeout"), HttpTransportFailure(S.SEND, R.TIMEOUT)),
        (httpx.WriteError("failed"), HttpTransportFailure(S.SEND, R.NETWORK_IO)),
        (httpx.ReadTimeout("timeout"), HttpTransportFailure(S.RECEIVE, R.TIMEOUT)),
        (httpx.ReadError("failed"), HttpTransportFailure(S.RECEIVE, R.NETWORK_IO)),
        (
            httpx.RemoteProtocolError("failed"),
            HttpTransportFailure(S.RECEIVE, R.PROTOCOL_VIOLATION),
        ),
        (httpx.CloseError("failed"), HttpTransportFailure(S.UNKNOWN, R.NETWORK_IO)),
    ],
)
def test_httpx_transport_failures(
    exc: Exception, expected: HttpTransportFailure
) -> None:
    """例外型から段階と理由を対応表で固定し、段階が読めない型は不明にする。"""
    assert classify_httpx(exc) == expected


@pytest.mark.parametrize(
    "exc",
    [
        httpx.TimeoutException("unspecified"),
        httpx.NetworkError("unspecified"),
        httpx.ProtocolError("unspecified"),
        httpx.TransportError("unspecified"),
    ],
)
def test_httpx_parent_transport_errors_are_unknown(exc: Exception) -> None:
    """具体型に落ちない通信失敗は段階も理由も unknown とする。"""
    assert classify_httpx(exc) == HttpTransportFailure(S.UNKNOWN, R.UNKNOWN)


@pytest.mark.parametrize(
    ("stage", "reached"),
    [
        (S.PREPARATION, False),
        (S.CONNECT, False),
        (S.SEND, True),
        (S.RECEIVE, True),
        (S.UNKNOWN, True),
    ],
)
def test_reachability_is_derived_from_stage_only(
    stage: HttpTransportStage, reached: bool
) -> None:
    """接続確立前だけ未達と断定し、理由や段階不明では到達を否定しない。"""
    for reason in R:
        assert (
            HttpTransportFailure(stage, reason).request_may_have_reached_server
            is reached
        )


@pytest.mark.parametrize(
    ("message", "proxy_status"),
    [
        pytest.param("403 Forbidden", 403, id="leading-403"),
        pytest.param("407 Proxy Authentication Required", 407, id="leading-407"),
        pytest.param("502 Bad Gateway", 502, id="leading-502"),
        pytest.param("503 Service Unavailable", 503, id="leading-503"),
        pytest.param("504 Gateway Timeout", 504, id="leading-504"),
        pytest.param("connection closed", None, id="no-status"),
        pytest.param("proxy returned 403 Forbidden", None, id="status-not-at-start"),
        pytest.param("4030 invalid", None, id="four-digits-are-not-status"),
    ],
)
def test_httpx_proxy_status_is_metadata_only(
    message: str, proxy_status: int | None
) -> None:
    """proxyのstatusによって段階や理由を変えない。"""
    assert classify_httpx(httpx.ProxyError(message)) == HttpTransportFailure(
        S.CONNECT, R.PROXY, proxy_status=proxy_status
    )


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("bad input"),
        RuntimeError("bug"),
        OSError("not necessarily network"),
        TimeoutError("unspecified operation"),
        HostBlockedError("policy"),
        httpx.LocalProtocolError("bad request"),
        httpx.UnsupportedProtocol("ftp"),
        httpx.InvalidURL("bad URL"),
        httpx.DecodingError("bad encoding"),
        httpx.TooManyRedirects("redirects"),
        httpx.RequestError("unspecified"),
        botocore_errors.ReadTimeoutError(endpoint_url="https://example.invalid"),
    ],
)
def test_httpx_does_not_classify_non_transport_errors(exc: Exception) -> None:
    """処理の文脈が必要な例外を通信失敗に丸めない。"""
    assert classify_httpx(exc) is None


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_http_status_errors_remain_outside_transport(status: int) -> None:
    """応答済みのHTTPエラーは呼び出し側に判断を残す。"""
    response = httpx.Response(
        status, request=httpx.Request("GET", "https://example.invalid")
    )
    with pytest.raises(httpx.HTTPStatusError) as caught:
        response.raise_for_status()
    assert classify_httpx(caught.value) is None


async def test_httpx_real_transport_classifies_dns_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """実際のhttpcoreとhttpxのラップを通してDNS失敗を識別する。"""
    monkeypatch.setattr(
        "anyio.connect_tcp", AsyncMock(side_effect=socket.gaierror("DNS failed"))
    )
    async with httpx.AsyncHTTPTransport() as transport:
        with pytest.raises(httpx.ConnectError) as caught:
            await transport.handle_async_request(
                httpx.Request("GET", "https://example.invalid")
            )
    assert classify_httpx(caught.value) == HttpTransportFailure(
        S.CONNECT, R.DNS_RESOLUTION
    )


async def test_httpx_real_transport_classifies_tls_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """実際のhttpcoreとhttpxのラップを通してTLS失敗を識別する。"""
    monkeypatch.setattr("anyio.connect_tcp", AsyncMock(return_value=AsyncMock()))
    monkeypatch.setattr(
        "anyio.streams.tls.TLSStream.wrap",
        AsyncMock(side_effect=ssl.SSLError("TLS failed")),
    )
    async with httpx.AsyncHTTPTransport() as transport:
        with pytest.raises(httpx.ConnectError) as caught:
            await transport.handle_async_request(
                httpx.Request("GET", "https://example.invalid")
            )
    assert classify_httpx(caught.value) == HttpTransportFailure(S.CONNECT, R.TLS)


def test_connection_cause_takes_precedence_over_unrelated_context() -> None:
    """明示的な原因があるときに別の処理で発生したcontextを採用しない。"""
    exc = httpx.ConnectError("failed")
    exc.__cause__ = ConnectionRefusedError("refused")
    exc.__context__ = socket.gaierror("unrelated DNS")
    assert classify_httpx(exc) == HttpTransportFailure(S.CONNECT, R.NETWORK_IO)


def test_connection_cause_cycle_terminates_without_mutating_exception() -> None:
    """循環する原因チェーンでも終了し、元の例外を変更しない。"""
    exc = httpx.ConnectError("failed")
    inner = OSError("failed")
    exc.__cause__ = inner
    inner.__context__ = exc
    assert classify_httpx(exc) == HttpTransportFailure(S.CONNECT, R.NETWORK_IO)
    assert exc.__cause__ is inner
    assert inner.__context__ is exc


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            botocore_errors.ConnectTimeoutError(endpoint_url="url"),
            HttpTransportFailure(S.CONNECT, R.TIMEOUT),
        ),
        (
            botocore_errors.EndpointConnectionError(endpoint_url="url"),
            HttpTransportFailure(S.CONNECT, R.NETWORK_IO),
        ),
        (
            botocore_errors.ProxyConnectionError(proxy_url="url"),
            HttpTransportFailure(S.CONNECT, R.PROXY),
        ),
        (
            botocore_errors.ReadTimeoutError(endpoint_url="url"),
            HttpTransportFailure(S.RECEIVE, R.TIMEOUT),
        ),
        (
            botocore_errors.SSLError(endpoint_url="url", error="TLS"),
            HttpTransportFailure(S.UNKNOWN, R.TLS),
        ),
        (
            botocore_errors.ConnectionClosedError(endpoint_url="url"),
            HttpTransportFailure(S.UNKNOWN, R.NETWORK_IO),
        ),
    ],
)
def test_botocore_transport_failures(
    exc: Exception, expected: HttpTransportFailure
) -> None:
    """具体型を親型より優先し、送受信どちらでも包まれる型は段階を断定しない。"""
    assert classify_botocore(exc) == expected


@pytest.mark.parametrize(
    "exc",
    [
        botocore_errors.HTTPClientError(error="unknown"),
        botocore_errors.ConnectionError(error="unknown"),
    ],
)
def test_botocore_parent_transport_errors_are_unknown(exc: Exception) -> None:
    """具体型に落ちない通信失敗は段階も理由も unknown とする。"""
    assert classify_botocore(exc) == HttpTransportFailure(S.UNKNOWN, R.UNKNOWN)


@pytest.mark.parametrize(
    "exc",
    [
        botocore_errors.NoCredentialsError(),
        botocore_errors.PartialCredentialsError(provider="test", cred_var="key"),
        botocore_errors.NoRegionError(),
        botocore_errors.ParamValidationError(report="invalid"),
        botocore_errors.BotoCoreError(),
        ValueError("invalid"),
        RuntimeError("bug"),
        OSError("unspecified"),
        httpx.ReadTimeout("other library"),
    ],
)
def test_botocore_does_not_classify_non_transport_errors(exc: Exception) -> None:
    """資格情報や入力不正を通信失敗に丸めない。"""
    assert classify_botocore(exc) is None


@pytest.mark.parametrize("code", ["AccessDenied", "RequestThrottled", "InternalError"])
def test_aws_service_errors_remain_outside_transport(code: str) -> None:
    """AWSのエラー応答の意味はpublisher側に判断を残す。"""
    exc = botocore_errors.ClientError({"Error": {"Code": code}}, "SendMessage")
    assert classify_botocore(exc) is None


def test_botocore_ssl_failure_while_reading_response_is_potentially_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """応答受信中のSSLエラーがbotocoreでラップされても未達扱いにしない。"""
    response = Mock(status=200, headers={})
    response.stream.side_effect = urllib3_errors.SSLError("bad record MAC")
    connection = Mock()
    connection.urlopen.return_value = response
    manager = Mock()
    manager.connection_from_url.return_value = connection
    request = AWSPreparedRequest(
        method="POST",
        url="https://example.invalid",
        headers={},
        body=b"{}",
        stream_output=False,
    )
    with closing(URLLib3Session(proxies={})) as session:
        monkeypatch.setattr(
            session, "_get_connection_manager", Mock(return_value=manager)
        )
        with pytest.raises(botocore_errors.SSLError) as caught:
            session.send(request)
    response.stream.assert_called_once()
    failure = classify_botocore(caught.value)
    assert failure == HttpTransportFailure(S.UNKNOWN, R.TLS)
    assert failure.request_may_have_reached_server is True


def test_transport_value_does_not_carry_sdk_message_or_url() -> None:
    """共通の値にはSDK例外に含まれる機密情報を持ち込まない。"""
    exc = botocore_errors.ProxyConnectionError(
        proxy_url="https://user:private-password@example.invalid"
    )
    failure = classify_botocore(exc)
    assert failure == HttpTransportFailure(S.CONNECT, R.PROXY)
    assert "private-password" not in repr(failure)
