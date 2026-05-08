# ruff: noqa: TID251
"""SSRF 検証付き ``httpx.AsyncClient`` のファクトリ。

外部 URL を fetch する経路は、すべてここを通すこと。``httpx.AsyncClient`` を
直接構築する経路は ``flake8-tidy-imports`` の ``TID251`` で禁止する
(``pyproject.toml`` 参照)。これにより:

- 「呼び出し側で ``ensure_host_is_public`` を呼び忘れる」運用ミスを構造的に排除
- リダイレクト経由 SSRF を default で遮断 (``follow_redirects=False``)
- DNS rebind / TOCTOU を Custom Transport の IP pin で構造的に閉塞
- 将来 fetch 箇所が増えても自動で同じ防御が効く

DNS pin の仕組み (red-team chain δ 対策):
    Transport ``_PinnedDnsTransport`` が ``handle_async_request`` の中で:
    1. URL host を 1 度だけ ``ensure_host_is_public`` で resolve + 全 IP public 検証
    2. 最初の resolved IP を URL host に書換 → TCP 接続は IP に対して張る
    3. ``request.headers["Host"]`` = 元 host (HTTP routing 用)
    4. ``request.extensions["sni_hostname"]`` = 元 host (TLS cert verify 用)

    httpcore 1.x が ``sni_hostname`` extension を ``ssl.server_hostname`` に
    渡すため、TCP は IP / TLS は元 host で完全分離する。validate と connect の
    間で DNS server が応答を切り替えても、TCP 接続先は validate 済の IP に
    pin されるため TOCTOU が成立しない。

例外フロー:
    transport の ``handle_async_request`` で raise した ``HostBlockedError`` /
    ``HostResolutionError`` は ``client.get()`` 等から呼び出し側にそのまま伝播
    する (httpx は wrap しない)。よって呼び出し側は既存の try/except に翻訳
    1 行ずつ追加するだけで Permanent/Temporary を切り分けられる。

呼び出し例:
    >>> async with make_safe_async_client(headers=HEADERS, timeout=30.0) as client:
    ...     try:
    ...         resp = await client.get(url)
    ...     except HostBlockedError as e:
    ...         raise PermanentFetchError(str(e)) from e
    ...     except HostResolutionError as e:
    ...         raise TemporaryFetchError(str(e)) from e
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

    ``handle_async_request`` を override し、リクエスト送信直前に DNS を 1 度だけ
    解決して全 IP が public であることを検証、最初の resolved IP を URL host に
    書換える。元 host は Host header と TLS SNI に保持するため、TCP 接続は IP /
    TLS は元 host の証明書で verify される構造的分離が成立する。

    本 transport を経由するリクエストは validate と TCP connect の間で DNS が
    切り替わっても影響を受けない (TOCTOU 不成立)。
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
