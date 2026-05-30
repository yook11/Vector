# ruff: noqa: TID251
"""SSRF 検証付き ``httpx.AsyncClient`` のファクトリ。

外部 URL を fetch する経路は、すべてここを通すこと。``httpx.AsyncClient`` を
直接構築する経路は ``flake8-tidy-imports`` の ``TID251`` で禁止する
(``pyproject.toml`` 参照)。これにより:

- 「呼び出し側で ``ensure_host_is_public`` を呼び忘れる」運用ミスを構造的に排除
- リダイレクト経由 SSRF を default で遮断 (``follow_redirects=False``)
- DNS rebind / TOCTOU を Custom Transport の IP pin で構造的に閉塞

DNS pin は ``_PinnedDnsTransport`` が送信直前に host を resolve し、全 IP を public
検証したうえで最初の IP へ TCP 接続先を固定する。Host header と TLS SNI は元 host
を保持するため、validate と connect の間で DNS 応答が変わっても TOCTOU が成立しない。

transport の ``HostBlockedError`` / ``HostResolutionError`` は httpx に wrap されず
呼び出し側へ伝播する。
"""

from __future__ import annotations

from typing import Any

import httpx

from app.shared.security.ssrf_guard import (
    HostBlockedError,
    NotAnIpAddressError,
    NotAPublicIpError,
    PublicIpAddress,
    ensure_host_is_public,
)

# AsyncHTTPTransport の constructor 引数のうち make_safe_async_client が
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


class _PinnedDnsTransport(httpx.AsyncHTTPTransport):
    """ssrf_guard で validate した IP に TCP 接続を pin する Transport。

    送信直前に DNS を解決して全 IP が public であることを検証し、TCP 接続先だけを
    検証済 IP に差し替える。Host header と TLS SNI は元 host に固定する。
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        original_host = request.url.host
        if not original_host:
            return await super().handle_async_request(request)

        # IP literal の場合: SafeUrl 構築時に PublicIpAddress で public 検証済の
        # 想定だが defense-in-depth でここでも判定する。private IP literal が
        # 直接渡されたら HostBlockedError で拒否。
        try:
            PublicIpAddress(original_host)
        except NotAnIpAddressError:
            # DNS 名 → resolve + pin に進む
            pass
        except NotAPublicIpError as e:
            msg = f"host is non-public IP literal: {original_host}"
            raise HostBlockedError(msg) from e
        else:
            return await super().handle_async_request(request)

        addrs = await ensure_host_is_public(original_host)
        # 最初の resolved IP に pin。multi-A / dual-stack でも全件 public 検証
        # 済なので 1 個目を選んで安全。
        pinned_ip = str(addrs[0])

        # Host header は元 host:port を保持 (HTTP routing / virtual host 用)。
        # netloc は IDNA encoded ASCII bytes で、port が default なら host のみ。
        original_host_header = request.url.netloc.decode("ascii")
        request.url = request.url.copy_with(host=pinned_ip)
        request.headers["Host"] = original_host_header
        # httpcore 1.x の sni_hostname extension で TLS server_hostname を
        # 元 host に固定 → IP に書換えても証明書の hostname verify が通る。
        request.extensions = {
            **request.extensions,
            "sni_hostname": original_host,
        }
        return await super().handle_async_request(request)


def make_safe_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """SSRF 検証 + DNS rebind 防御入りの ``httpx.AsyncClient`` を返す。

    - DNS resolution と IP pin を ``_PinnedDnsTransport`` に統合 (TOCTOU 不成立)
    - ``follow_redirects`` は明示指定がなければ ``False`` を default 適用
      (Location 先で再度 DNS 検証を行わないため、信頼境界を超えない方針)
    - transport-level kwargs (``verify`` / ``cert`` / ``http1`` / ``http2`` /
      ``limits`` / ``trust_env`` / ``proxy`` / ``uds`` / ``local_address`` /
      ``retries`` / ``socket_options``) は transport コンストラクタに振分
    - 残りの kwargs (``headers`` / ``timeout`` / ``follow_redirects`` 等) は
      ``httpx.AsyncClient`` にそのまま委譲する
    """
    kwargs.setdefault("follow_redirects", False)

    transport_kwargs = {k: kwargs.pop(k) for k in _TRANSPORT_KEYS if k in kwargs}
    transport = _PinnedDnsTransport(**transport_kwargs)

    return httpx.AsyncClient(transport=transport, **kwargs)
