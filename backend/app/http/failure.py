"""単一の通信試行で応答の受信を完了できなかった失敗を、段階と理由で表す。

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


class HttpTransportStage(StrEnum):
    """通信失敗が起きた段階で、送信側か受信側かはここから読む。"""

    PREPARATION = "preparation"
    """接続の空き待ちや事前の名前解決検証など、接続を試みる前のクライアント内。"""
    CONNECT = "connect"
    """名前解決、TCP接続、TLS、proxyトンネル確立。"""
    SEND = "send"
    """リクエストの書き込み中。"""
    RECEIVE = "receive"
    """応答待ち、応答の読み取り、HTTPプロトコルの解釈中。"""
    UNKNOWN = "unknown"
    """SDKの例外型からは段階を特定できない。"""


class HttpTransportFailureReason(StrEnum):
    """通信失敗として確認できた事象で、段階の情報は含まない。"""

    TIMEOUT = "timeout"
    DNS_RESOLUTION = "dns_resolution"
    TLS = "tls"
    PROXY = "proxy"
    """proxy接続またはトンネル確立に失敗した。取得先の応答ではない。"""
    NETWORK_IO = "network_io"
    """接続失敗、切断、読み書き失敗などソケットレベルの失敗。"""
    PROTOCOL_VIOLATION = "protocol_violation"
    """相手側のHTTPプロトコル違反。"""
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class HttpTransportFailure:
    """どの段階で何が起きたかを保持する不変の値。

    送信先はproxyの先の対象サービスを指す。
    SDK内部の再試行を含む呼び出し全体の送達結果は、この値だけでは判断できない。
    """

    stage: HttpTransportStage
    reason: HttpTransportFailureReason
    proxy_status: int | None = None
    """proxyが拒否時に返したHTTP statusで、取得できない場合や対象外はNoneとする。"""

    @property
    def request_may_have_reached_server(self) -> bool:
        """接続確立前の失敗だけ未達と断定でき、段階不明は到達を否定できない。"""
        return self.stage not in (
            HttpTransportStage.PREPARATION,
            HttpTransportStage.CONNECT,
        )


_PROXY_REFUSAL_STATUS = re.compile(r"^(\d{3})\b")


def _connect_failure_reason(exc: httpx.ConnectError) -> HttpTransportFailureReason:
    """httpcoreによる多段ラップの内側から接続失敗の原因を読む。"""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, socket.gaierror):
            return HttpTransportFailureReason.DNS_RESOLUTION
        if isinstance(current, ssl.SSLError):
            return HttpTransportFailureReason.TLS
        # httpcoreのraise from Noneでcauseが消えても元の原因はcontextに残る。
        current = (
            current.__cause__ if current.__cause__ is not None else current.__context__
        )
    return HttpTransportFailureReason.NETWORK_IO


def classify_httpx(exc: Exception) -> HttpTransportFailure | None:
    """httpxとDNS事前検証の通信失敗を分類し、対象外の例外にはNoneを返す。

    RemoteProtocolErrorをRECEIVEとするのはHTTP/1.1経路の前提で、
    HTTP/2を有効にする場合は送信中のGOAWAY等を含めて段階を再確認する。
    """
    if isinstance(exc, HostResolutionError):
        return HttpTransportFailure(
            HttpTransportStage.PREPARATION, HttpTransportFailureReason.DNS_RESOLUTION
        )
    if isinstance(exc, httpx.LocalProtocolError | httpx.UnsupportedProtocol):
        return None
    if isinstance(exc, httpx.PoolTimeout):
        return HttpTransportFailure(
            HttpTransportStage.PREPARATION, HttpTransportFailureReason.TIMEOUT
        )
    if isinstance(exc, httpx.ConnectTimeout):
        return HttpTransportFailure(
            HttpTransportStage.CONNECT, HttpTransportFailureReason.TIMEOUT
        )
    if isinstance(exc, httpx.WriteTimeout):
        return HttpTransportFailure(
            HttpTransportStage.SEND, HttpTransportFailureReason.TIMEOUT
        )
    if isinstance(exc, httpx.ReadTimeout):
        return HttpTransportFailure(
            HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
        )
    if isinstance(exc, httpx.ConnectError):
        return HttpTransportFailure(
            HttpTransportStage.CONNECT, _connect_failure_reason(exc)
        )
    if isinstance(exc, httpx.ProxyError):
        # httpcoreのCONNECT拒否は構造化statusを持たず、メッセージ先頭に3桁で現れる。
        match = _PROXY_REFUSAL_STATUS.match(str(exc))
        return HttpTransportFailure(
            HttpTransportStage.CONNECT,
            HttpTransportFailureReason.PROXY,
            proxy_status=int(match.group(1)) if match else None,
        )
    if isinstance(exc, httpx.RemoteProtocolError):
        return HttpTransportFailure(
            HttpTransportStage.RECEIVE, HttpTransportFailureReason.PROTOCOL_VIOLATION
        )
    if isinstance(exc, httpx.WriteError):
        return HttpTransportFailure(
            HttpTransportStage.SEND, HttpTransportFailureReason.NETWORK_IO
        )
    if isinstance(exc, httpx.ReadError):
        return HttpTransportFailure(
            HttpTransportStage.RECEIVE, HttpTransportFailureReason.NETWORK_IO
        )
    if isinstance(exc, httpx.CloseError):
        return HttpTransportFailure(
            HttpTransportStage.UNKNOWN, HttpTransportFailureReason.NETWORK_IO
        )
    if isinstance(exc, httpx.TransportError):
        return HttpTransportFailure(
            HttpTransportStage.UNKNOWN, HttpTransportFailureReason.UNKNOWN
        )
    return None


def classify_botocore(exc: Exception) -> HttpTransportFailure | None:
    """botocoreの通信失敗を分類し、応答エラーや資格情報などは対象外とする。

    最後の例外だけではSDK内部再試行を含む呼び出し全体の未達は保証できない。
    """
    if isinstance(exc, botocore_errors.ConnectTimeoutError):
        return HttpTransportFailure(
            HttpTransportStage.CONNECT, HttpTransportFailureReason.TIMEOUT
        )
    if isinstance(exc, botocore_errors.ReadTimeoutError):
        return HttpTransportFailure(
            HttpTransportStage.RECEIVE, HttpTransportFailureReason.TIMEOUT
        )
    if isinstance(exc, botocore_errors.EndpointConnectionError):
        return HttpTransportFailure(
            HttpTransportStage.CONNECT, HttpTransportFailureReason.NETWORK_IO
        )
    if isinstance(exc, botocore_errors.SSLError):
        # botocoreは応答読み取り中のSSLエラーも同じ型に包むため段階を断定しない。
        return HttpTransportFailure(
            HttpTransportStage.UNKNOWN, HttpTransportFailureReason.TLS
        )
    if isinstance(exc, botocore_errors.ProxyConnectionError):
        return HttpTransportFailure(
            HttpTransportStage.CONNECT, HttpTransportFailureReason.PROXY
        )
    if isinstance(exc, botocore_errors.ConnectionClosedError):
        # urllib3は送信中のOSErrorもProtocolErrorに包むため受信段階と断定しない。
        return HttpTransportFailure(
            HttpTransportStage.UNKNOWN, HttpTransportFailureReason.NETWORK_IO
        )
    if isinstance(
        exc, botocore_errors.HTTPClientError | botocore_errors.ConnectionError
    ):
        return HttpTransportFailure(
            HttpTransportStage.UNKNOWN, HttpTransportFailureReason.UNKNOWN
        )
    return None
