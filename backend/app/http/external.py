"""第三者宛の標準transportに、送信時の宛先検証とプロキシ経路を組み込む。

IP直書きは ``destination_policy``、DNS名は ``destination_resolution`` で検証する。
通常のファクトリ経路は ``HttpSettings.egress_proxy_url`` が必須で、元のホスト名を
プロキシへ渡すため、実際の接続先の非公開IP拒否はプロキシ自身が担当する。
プロキシは元のホスト名で許可ドメインを判定するため、プロキシ経路ではIPに書き換えない。

既存transportを直接接続で使う場合は、検証した最初のIPへ接続先を固定し、
HostとTLS SNIは元のホスト名を保持するが、ファクトリには直接接続へのfallbackはない。
標準transportはHTTP・HTTPS以外を送信せず、リダイレクト先も検証し、追従の既定値はFalseとする。

``HostBlockedError`` と ``HostResolutionError`` は元のまま利用機能へ伝播する。
ファクトリは ``mounts``・``transport`` の指定を拒否し、送信部品の差し替えを許可しない。
"""

from __future__ import annotations

from typing import Any

import httpx2

from app.http.destination_policy import (
    HostBlockedError,
    NotAnIpAddressError,
    NotAPublicIpError,
    PublicIpAddress,
)
from app.http.destination_resolution import resolve_public_host_addresses
from app.http.settings import HttpSettings
from app.http.tls import create_verification_context

# AsyncHTTPTransport の constructor 引数のうち make_external_async_client が
# kwargs から取り分けて transport に渡す key 群。
_TRANSPORT_KEYS: tuple[str, ...] = (
    "verify",
    "cert",
    "trust_env",
    "http1",
    "http2",
    "limits",
    "proxy",
    "uds",
    "local_address",
    "retries",
    "socket_options",
)

_SENDABLE_SCHEMES: frozenset[str] = frozenset({"http", "https"})


def _pin_connection_to_address(
    request: httpx2.Request, address: PublicIpAddress, original_host: str
) -> None:
    """TCP 接続先だけを検証済 IP に差し替える。

    ``address`` の型が「public 検証を通った」ことを表すので、検証を経ていない値を
    ここに渡す手段が存在しない。
    """
    # Host header は元 host:port を保持 (HTTP routing / virtual host 用)。
    # netloc は IDNA encoded ASCII bytes で、port が default なら host のみ。
    original_host_header = request.url.netloc.decode("ascii")
    request.url = request.url.copy_with(host=str(address))
    request.headers["Host"] = original_host_header
    # httpcore2 の sni_hostname extension で TLS server_hostname を
    # 元 host に固定 → IP に書換えても証明書の hostname verify が通る。
    request.extensions = {
        **request.extensions,
        "sni_hostname": original_host,
    }


class _PinnedDnsTransport(httpx2.AsyncHTTPTransport):
    """送信直前に宛先方針を適用し、直接接続の場合だけ検証済みIPへ固定する。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # 経路が proxy かどうかは構築時に確定するので、ここで一度だけ導出する。
        # フラグを外から受け取らないのは、設定と実際の経路がずれる余地を残さないため。
        self._pins_connection = kwargs.get("proxy") is None

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        # HTTPX2/httpcore2はws・wssも送信するため、名前解決より前に限定する。
        if request.url.scheme not in _SENDABLE_SCHEMES:
            msg = f"external HTTP sends only http or https: {request.url.scheme!r}"
            raise httpx2.UnsupportedProtocol(msg, request=request)

        original_host = request.url.host
        if not original_host:
            return await super().handle_async_request(request)

        # URL型は宛先IPを判定しないため、送信時にIP直書きを検証する。
        try:
            PublicIpAddress(original_host)
        except NotAnIpAddressError:
            # DNS名は解決結果へIP方針を適用し、接続先の固定は経路に従う。
            pass
        except NotAPublicIpError as e:
            msg = f"host is non-public IP literal: {original_host}"
            raise HostBlockedError(msg) from e
        else:
            return await super().handle_async_request(request)

        addrs = await resolve_public_host_addresses(original_host)
        if self._pins_connection:
            # 最初の resolved IP に pin。multi-A / dual-stack でも全件 public 検証
            # 済なので 1 個目を選んで安全。
            _pin_connection_to_address(request, addrs[0], original_host)
        return await super().handle_async_request(request)


def make_external_async_client(**kwargs: Any) -> httpx2.AsyncClient:  # noqa: TID251
    """宛先検証を持つ標準transportと、設定で確定したプロキシを使用する。

    許可ドメインは送信元の実行単位に対応するプロキシ設定が管理する。
    元のホスト名を渡すため、プロキシ自身の接続先にもIP制限が必要になる。
    リダイレクト追従は既定でFalseとし、明示的なTrueは維持する。
    transport用の引数を取り分け、残りの引数はHTTPX2へ委譲する。
    標準transportの経路と証明書の検証基準は設定が決め、呼び出し側のproxy・verify引数は上書きする。
    mounts・transportの指定は値によらず、transport生成前にTypeErrorで拒否する。
    """
    if "mounts" in kwargs or "transport" in kwargs:
        msg = "external HTTP transport and mounts cannot be overridden"
        raise TypeError(msg)

    kwargs.setdefault("follow_redirects", False)
    # 標準transportの出口は実行環境が決め、設定不備はtransport生成前に拒否する。
    proxy_url = HttpSettings().egress_proxy_url  # type: ignore[call-arg]
    # httpcore2はhttpのプロキシへの検証contextを拒否するため、httpsのときだけ渡す。
    kwargs["proxy"] = (
        httpx2.Proxy(proxy_url, ssl_context=create_verification_context())
        if httpx2.URL(proxy_url).scheme == "https"
        else proxy_url
    )
    kwargs["verify"] = create_verification_context()

    transport_kwargs = {k: kwargs.pop(k) for k in _TRANSPORT_KEYS if k in kwargs}
    transport = _PinnedDnsTransport(**transport_kwargs)

    return httpx2.AsyncClient(transport=transport, **kwargs)  # noqa: TID251
