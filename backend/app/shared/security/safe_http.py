# ruff: noqa: TID251
"""SSRF 検証付き ``httpx.AsyncClient`` のファクトリ。

外部 URL を fetch する経路は、すべてここを通すこと。``httpx.AsyncClient`` を
直接構築する経路は ``flake8-tidy-imports`` の ``TID251`` で禁止する
(``pyproject.toml`` 参照)。これにより:

- 「呼び出し側で ``ensure_host_is_public`` を呼び忘れる」運用ミスを構造的に排除
- リダイレクト経由 SSRF を default で遮断 (``follow_redirects=False``)
- 将来 fetch 箇所が増えても自動で同じ防御が効く

例外フロー:
    httpx の ``event_hooks["request"]`` で raise した例外は、``client.get()`` 等の
    呼び出し経由でそのまま伝播する (httpx は wrap しない)。よって呼び出し側は
    既存の try/except 節に ``HostBlockedError`` / ``HostResolutionError`` の
    翻訳を 1 行ずつ追加するだけで適切な Permanent/Temporary を切り分けられる。

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

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import httpx

from app.shared.security.ssrf_guard import ensure_host_is_public

RequestHook = Callable[[httpx.Request], Awaitable[Any]]


async def _validate_request(request: httpx.Request) -> None:
    """全リクエストに対し送信先ホストが public IP に解決されることを保証する。

    httpx の event_hook として登録されるため、``client.get`` / ``client.post``
    などすべてのリクエスト直前で起動する。``ensure_host_is_public`` の
    raise する ``HostBlockedError`` / ``HostResolutionError`` は呼び出し側に
    そのまま伝播する。
    """
    host = request.url.host
    if host:
        await ensure_host_is_public(host)


def make_safe_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """SSRF 検証 event_hook を組み込んだ ``httpx.AsyncClient`` を返す。

    - ``follow_redirects`` は明示指定がなければ ``False`` を default 適用
      (リダイレクト先の Location は再度 DNS 検証なしには辿れないため)
    - 呼び出し側が ``event_hooks={"request": [...]}`` を渡した場合は
      ``_validate_request`` を先頭に付けて merge する (既存 hook は壊さない)
    - その他のキーワード引数 (``headers``, ``timeout``, ``verify`` 等) は
      ``httpx.AsyncClient`` にそのまま委譲する
    """
    kwargs.setdefault("follow_redirects", False)

    user_event_hooks: dict[str, Iterable[Callable[..., Any]]] = (
        kwargs.pop("event_hooks", None) or {}
    )
    user_request_hooks = list(user_event_hooks.get("request", []))
    other_hooks = {k: list(v) for k, v in user_event_hooks.items() if k != "request"}

    merged_hooks: dict[str, list[Callable[..., Any]]] = {
        "request": [_validate_request, *user_request_hooks],
        **other_hooks,
    }

    return httpx.AsyncClient(event_hooks=merged_hooks, **kwargs)
