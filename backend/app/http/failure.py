"""単一の通信試行で起きた失敗と、送信先へのリクエスト到達可能性を表す。

HTTPエラー応答、資格情報不足、SSRF拒否、不正なURLやリクエスト生成は対象外。
ライブラリ例外の分類結果を各文脈の例外へ載せる値であり、retryやholdは判断しない。
"""

import re
import socket
import ssl
from dataclasses import dataclass
from enum import StrEnum

import httpx
from botocore import exceptions as botocore_errors

from app.shared.security.ssrf_guard import HostResolutionError


class HttpTransportFailureKind(StrEnum):
    """通信失敗として確認できた事象の種類。"""

    DNS_RESOLUTION = "dns_resolution"
    """接続先の名前解決に失敗した。"""
    CONNECT = "connect"
    """接続を確立できなかった。"""
    CONNECT_TIMEOUT = "connect_timeout"
    """接続確立が時間切れになった。"""
    POOL_TIMEOUT = "pool_timeout"
    """クライアント側で接続の空きを待って時間切れになった。"""
    TLS = "tls"
    """接続確立時または通信中にTLS関連の失敗が起きた。"""
    PROXY = "proxy"
    """proxy接続またはトンネル確立に失敗した。"""
    WRITE_TIMEOUT = "write_timeout"
    """リクエストの送信中に時間切れになった。"""
    READ_TIMEOUT = "read_timeout"
    """応答の受信待ちまたは受信中に時間切れになった。"""
    NETWORK_IO = "network_io"
    """切断を含む通信の読み書きまたは接続終了に失敗した。"""
    REMOTE_PROTOCOL = "remote_protocol"
    """相手側とのHTTP通信でプロトコル違反が起きた。"""
    UNKNOWN = "unknown"
    """通信失敗と確認できるが詳細を特定できなかった。"""


@dataclass(frozen=True, slots=True)
class HttpTransportFailure:
    """分類根拠に応じて種類と到達可能性を別々に保持する不変の値。

    送信先はproxyの先の対象サービスを指し、到達可能性は処理完了を保証しない。
    SDK内部の再試行を含む呼び出し全体の送達結果は、この値だけでは判断できない。
    TLSなどは発生段階によって到達可能性が異なるため、kindから自動導出しない。
    """

    kind: HttpTransportFailureKind
    request_may_have_reached_server: bool
    """Falseは当該試行で未達と判断でき、Trueは到達の可能性を否定できないことを表す。"""
    proxy_status: int | None = None
    """proxyが接続拒否時に返したHTTP statusで、取得できない場合や対象外はNoneとする。"""


_PROXY_REFUSAL_STATUS = re.compile(r"^(\d{3})\b")


def _connect_failure_kind(exc: httpx.ConnectError) -> HttpTransportFailureKind:
    """httpcoreによる多段ラップの内側から接続失敗の原因を読む。"""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, socket.gaierror):
            return HttpTransportFailureKind.DNS_RESOLUTION
        if isinstance(current, ssl.SSLError):
            return HttpTransportFailureKind.TLS
        # httpcoreのraise from Noneでcauseが消えても元の原因はcontextに残る。
        current = (
            current.__cause__ if current.__cause__ is not None else current.__context__
        )
    return HttpTransportFailureKind.CONNECT


def classify_httpx(exc: Exception) -> HttpTransportFailure | None:
    """httpxとDNS事前検証の通信失敗を分類し、対象外の例外にはNoneを返す。"""
    if isinstance(exc, HostResolutionError):
        return HttpTransportFailure(HttpTransportFailureKind.DNS_RESOLUTION, False)
    if isinstance(exc, httpx.LocalProtocolError | httpx.UnsupportedProtocol):
        return None
    if isinstance(exc, httpx.ConnectTimeout):
        return HttpTransportFailure(HttpTransportFailureKind.CONNECT_TIMEOUT, False)
    if isinstance(exc, httpx.PoolTimeout):
        return HttpTransportFailure(HttpTransportFailureKind.POOL_TIMEOUT, False)
    if isinstance(exc, httpx.WriteTimeout):
        return HttpTransportFailure(HttpTransportFailureKind.WRITE_TIMEOUT, True)
    if isinstance(exc, httpx.ReadTimeout):
        return HttpTransportFailure(HttpTransportFailureKind.READ_TIMEOUT, True)
    if isinstance(exc, httpx.ConnectError):
        return HttpTransportFailure(_connect_failure_kind(exc), False)
    if isinstance(exc, httpx.ProxyError):
        # httpcoreのCONNECT拒否は構造化statusを持たず、メッセージ先頭に3桁で現れる。
        match = _PROXY_REFUSAL_STATUS.match(str(exc))
        return HttpTransportFailure(
            HttpTransportFailureKind.PROXY,
            False,
            proxy_status=int(match.group(1)) if match else None,
        )
    if isinstance(exc, httpx.RemoteProtocolError):
        return HttpTransportFailure(HttpTransportFailureKind.REMOTE_PROTOCOL, True)
    if isinstance(exc, httpx.ReadError | httpx.WriteError | httpx.CloseError):
        return HttpTransportFailure(HttpTransportFailureKind.NETWORK_IO, True)
    if isinstance(exc, httpx.TransportError):
        return HttpTransportFailure(HttpTransportFailureKind.UNKNOWN, True)
    return None


def classify_botocore(exc: Exception) -> HttpTransportFailure | None:
    """botocoreの通信失敗を分類し、応答エラーや資格情報などは対象外とする。

    最後の例外だけではSDK内部再試行を含む呼び出し全体の未達は保証できない。
    """
    if isinstance(exc, botocore_errors.ConnectTimeoutError):
        return HttpTransportFailure(HttpTransportFailureKind.CONNECT_TIMEOUT, False)
    if isinstance(exc, botocore_errors.ReadTimeoutError):
        return HttpTransportFailure(HttpTransportFailureKind.READ_TIMEOUT, True)
    if isinstance(exc, botocore_errors.EndpointConnectionError):
        return HttpTransportFailure(HttpTransportFailureKind.CONNECT, False)
    if isinstance(exc, botocore_errors.SSLError):
        # botocoreは応答読み取り中のSSLエラーも同じ型に包むため未達とは断定しない。
        return HttpTransportFailure(HttpTransportFailureKind.TLS, True)
    if isinstance(exc, botocore_errors.ProxyConnectionError):
        return HttpTransportFailure(HttpTransportFailureKind.PROXY, False)
    if isinstance(exc, botocore_errors.ConnectionClosedError):
        return HttpTransportFailure(HttpTransportFailureKind.NETWORK_IO, True)
    if isinstance(
        exc, botocore_errors.HTTPClientError | botocore_errors.ConnectionError
    ):
        return HttpTransportFailure(HttpTransportFailureKind.UNKNOWN, True)
    return None
