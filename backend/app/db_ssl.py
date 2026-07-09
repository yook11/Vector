"""接続文字列から SSL 設定を分離する純粋ヘルパー + engine factory。

backend (SQLAlchemy + asyncpg) を Neon 等の managed Postgres に verify-full
(CA + ホスト名検証) で繋ぐための一元化層。frontend の
``frontend/src/lib/auth/pool-ssl.ts`` を backend に対称移植したもの。

設計:
- asyncpg は ``sslmode`` / ``channel_binding`` / ``ssl`` / ``sslrootcert`` 等を
  kwarg で受けず、URL 由来の query が SQLAlchemy 経由で ``asyncpg.connect`` に
  そのまま渡ると connect 時に ``TypeError`` になる。よって URL から ssl 系
  param を取り除き、SSL は ``connect_args={"ssl": SSLContext}`` に正規化する。
- ``ssl.create_default_context`` は ``CERT_REQUIRED`` + ``check_hostname=True``
  (= verify-full 相当)。CA は ``certifi`` バンドルを明示する
  (asyncpg 0.31 は ``sslrootcert=system`` 非対応)。
- ``sslmode=require`` でも verify-full に格上げする。Fly.io → Neon は public
  internet を通るため検証は必須で、Neon は require でも TLS のため実害なし。
  **平文にしたいのは ``sslmode=disable`` のときだけ**。TLS-without-verification
  モードは設計上存在しない。

接続文字列のみで dev (docker, sslmode 無し → SSL 無効) と本番 (Neon,
``?sslmode=require`` → verify-full) を切り替えられる。

import は標準ライブラリ + ``certifi`` + ``sqlalchemy`` のみに閉じる
(``app.config`` も ``app.db`` も import しない)。これにより alembic / scripts が
factory を import しても設定副作用も循環依存も起きない。
"""

from __future__ import annotations

import ssl
from typing import Any

import certifi
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

# libpq 互換の sslmode allowlist。allowlist 外 (typo) は ValueError で弾く。
_VALID_SSLMODES = frozenset(
    {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
)

# URL から取り除く ssl 系 query param。ひとつでも残ると asyncpg.connect が
# TypeError を出すため、SQLAlchemy URL から network 接続前に剥がす。
_SSL_QUERY_PARAMS = (
    "sslmode",
    "channel_binding",
    "ssl",
    "sslrootcert",
    "sslcert",
    "sslkey",
    "sslcrl",
)
_SSL_QUERY_PARAM_SET = frozenset(_SSL_QUERY_PARAMS)

# [P1] guard から除外する param。sslmode は signal そのもの、channel_binding は
# Neon ネイティブ文字列で sslmode と共存するため。
_SSL_PARAMS_GUARD_EXEMPT = frozenset({"sslmode", "channel_binding"})

# sslmode 抜きで単独指定されると「SSL のつもりが平文化」を招く ssl 系 param。
# これらが在って sslmode が無い場合は黙って剥がさず ValueError で落とす。
# _SSL_QUERY_PARAMS から派生させ、将来 ssl param を足したら自動で guard 対象に
# なる (未知 param は fail-loud がデフォルト = silent な平文降格を構造的に防ぐ)。
_SSL_PARAMS_REQUIRING_SSLMODE = frozenset(
    p for p in _SSL_QUERY_PARAMS if p not in _SSL_PARAMS_GUARD_EXEMPT
)


def parse_sslmode(raw_url: str) -> str | None:
    """接続文字列の ``sslmode`` 値を取り出し allowlist で検証する純粋関数。

    値が無ければ ``None``。allowlist 外 (``sslmode=requrie`` 等の typo) は
    ``ValueError``。config validator と factory が共有する sslmode 解釈の SSoT。

    libpq の param は小文字だが、大文字変種 (``?SSLMODE=``) を取りこぼして平文
    降格しないよう key・value とも大小無視で解釈する。
    """
    values = [v for k, v in make_url(raw_url).query.items() if k.lower() == "sslmode"]
    if not values:
        return None
    # 同一キーの複数指定 (SQLAlchemy は tuple 化) も大小違いの重複 (sslmode &
    # SSLMODE) も「高々 1 回」違反として弾く。
    if len(values) > 1 or not isinstance(values[0], str):
        raise ValueError(f"sslmode must be specified at most once, got: {values!r}")
    sslmode = values[0].lower()
    if sslmode not in _VALID_SSLMODES:
        raise ValueError(
            f"invalid sslmode {values[0]!r}; expected one of "
            f"{sorted(_VALID_SSLMODES)} (check for typos)"
        )
    return sslmode


def split_ssl_from_url(raw_url: str) -> tuple[str, dict[str, Any]]:
    """接続文字列を ``(clean_url, connect_args)`` に分解する純粋関数。

    - ssl 系 query param を URL から除去する (残ると asyncpg.connect が TypeError)。
    - ``sslmode`` が ``disable`` 以外なら verify-full の ``SSLContext`` を
      ``connect_args["ssl"]`` に載せる。``None`` / ``disable`` なら ``{}``。
    - ``sslmode`` 抜きで ``ssl`` / ``sslrootcert`` 等が単独指定された場合は黙って
      平文化せず ``ValueError`` で落とす (「SSL は ?sslmode=require で指定する」)。

    ssl 系**以外**の query は変更しない。
    """
    sslmode = parse_sslmode(raw_url)

    url = make_url(raw_url)
    query = url.query
    if sslmode is None:
        # key は大小無視で判定 (?SSLROOTCERT= 等の大文字変種も取りこぼさない)。
        offending = sorted(
            {k for k in query if k.lower() in _SSL_PARAMS_REQUIRING_SSLMODE}
        )
        if offending:
            raise ValueError(
                f"connection string sets {offending} without sslmode; "
                "specify SSL via `?sslmode=require` (asyncpg ignores raw ssl "
                "params and would otherwise connect in plaintext)"
            )

    # 大文字変種も含め ssl 系 key を実キー名で除去 (残ると asyncpg.connect が
    # TypeError)。ssl 系**以外**の query は変更しない。
    ssl_keys = [k for k in query if k.lower() in _SSL_QUERY_PARAM_SET]
    clean_url = url.difference_update_query(ssl_keys).render_as_string(
        # hide_password=True (default) は password を *** に伏せて接続不能にする。
        hide_password=False,
    )

    connect_args: dict[str, Any] = {}
    if sslmode is not None and sslmode != "disable":
        connect_args["ssl"] = _verify_full_context()
    return clean_url, connect_args


def clean_db_url(raw_url: str) -> str:
    """ssl 系 param を剥がした接続文字列を返す (接続しない alembic offline 用)。"""
    return split_ssl_from_url(raw_url)[0]


DEFAULT_POOL_RECYCLE = 3600  # 古い接続を proactive 破棄 (Neon autosuspend 対策)
DEFAULT_POOL_TIMEOUT = 5  # QueuePool 飽和時の fail-fast (SQLAlchemy 既定 30s でなく)


def _merge_server_settings(
    connect_args: dict[str, Any], application_name: str | None
) -> dict[str, Any]:
    """``application_name`` を asyncpg の ``server_settings`` に注入する。

    asyncpg は ``application_name`` 専用 kwarg を持たないため
    ``server_settings`` 経由で渡す。既存の ``server_settings`` は保持し、
    ``application_name`` kwarg を優先する。
    """
    if application_name is None:
        return connect_args
    server_settings = {
        **connect_args.get("server_settings", {}),
        "application_name": application_name,  # PostgreSQL の上限は 63 バイト
    }
    return {**connect_args, "server_settings": server_settings}


def create_app_engine(
    url: str, *, application_name: str | None = None, **engine_kwargs: Any
) -> AsyncEngine:
    """SSL と application_name を一元注入する唯一の engine 生成入口。

    URL から ssl 系 param を剥がし、SSL 要時は verify-full の ``SSLContext`` を
    ``connect_args`` に注入して ``create_async_engine`` を呼ぶ。SSL の決定権を
    構造的に一元化するため、呼び出し側が ``connect_args["ssl"]`` を渡したら
    ``ValueError`` で fail-fast する (ssl 以外の connect_args はマージ保持)。
    ``application_name`` は asyncpg の ``server_settings`` に焼く。
    """
    clean_url, ssl_connect_args = split_ssl_from_url(url)

    caller_connect_args = dict(engine_kwargs.pop("connect_args", {}))
    if "ssl" in caller_connect_args:
        raise ValueError(
            "connect_args['ssl'] must not be passed to create_app_engine; "
            "SSL is derived from the connection string's sslmode (single source "
            "of truth). Use `?sslmode=require` instead."
        )
    merged_connect_args = {**caller_connect_args, **ssl_connect_args}
    merged_connect_args = _merge_server_settings(merged_connect_args, application_name)

    # Neon scale-to-zero (autosuspend) で idle 接続が切られるため、全 engine に
    # stale-connection resilience を既定付与する (呼び出し側が明示すれば override)。
    engine_kwargs.setdefault("pool_pre_ping", True)  # checkout 時 liveness check
    engine_kwargs.setdefault("pool_recycle", DEFAULT_POOL_RECYCLE)
    engine_kwargs.setdefault("hide_parameters", True)
    # pool_timeout は QueuePool 専用。NullPool 移行時は付与しない
    # (neon-connection-routing.md の pooler 昇格手順)。
    if engine_kwargs.get("poolclass") is not NullPool:
        engine_kwargs.setdefault("pool_timeout", DEFAULT_POOL_TIMEOUT)

    return create_async_engine(
        clean_url, connect_args=merged_connect_args, **engine_kwargs
    )


def _verify_full_context() -> ssl.SSLContext:
    """verify-full (CA + ホスト名検証) の ``SSLContext`` を都度生成する。

    ``create_default_context`` は ``CERT_REQUIRED`` + ``check_hostname=True``。
    CA は certifi バンドルを明示する (asyncpg 0.31 は ``sslrootcert=system``
    非対応)。**module キャッシュしない**: fork する worker で親→子に OpenSSL
    state を共有させないため (engine は WORKER_STARTUP = fork 後生成だが、
    module-level singleton を避けて親で先に作られる経路自体を排除する)。
    engine 生成はプロセス毎に数回のみで CA 読み込みコストは無視できる。
    """
    return ssl.create_default_context(cafile=certifi.where())
