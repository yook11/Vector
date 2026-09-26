"""接続文字列からSSL設定を分離する純粋ヘルパー。

backend (SQLAlchemy + asyncpg) を RDS に verify-full (CA + ホスト名検証) で
繋ぐための一元化層。frontend の
``frontend/src/lib/auth/pool-ssl.ts`` を backend に対称移植したもの。

設計:
- asyncpg は ``sslmode`` / ``ssl`` / ``sslrootcert`` 等を
  kwarg で受けず、URL 由来の query が SQLAlchemy 経由で ``asyncpg.connect`` に
  そのまま渡ると connect 時に ``TypeError`` になる。よって URL から ssl 系
  param を取り除き、SSL は ``connect_args={"ssl": SSLContext}`` に正規化する。
- ``ssl.create_default_context`` は ``CERT_REQUIRED`` + ``check_hostname=True``
  (= verify-full 相当)。CA は RDS の regional bundle (root 3 本) だけを信頼し、
  公開の認証局は信頼しない。
- ``sslmode=require`` でも verify-full に格上げする。
  **平文にしたいのは ``sslmode=disable`` のときだけ**。TLS-without-verification
  モードは設計上存在しない。

接続文字列のみで dev (docker, sslmode 無し → SSL 無効) と本番 (RDS,
``?sslmode=require`` → verify-full) を切り替えられる。

import は標準ライブラリ + ``sqlalchemy`` のみに閉じるため、
設定読込の副作用や循環依存なしにEngine生成とmigrationから共有できる。
"""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url

# DB 接続で信頼するのはこの RDS の root だけ。RDS の証明書は AWS 独自の private root
# が発行するので、公開の認証局は検証に使わない。
#
# 使うリージョンの 3 root (RSA2048 / RSA4096 / ECC384) だけを入れる。global bundle は
# 全リージョン 108 root を無条件に信頼することになるので採らない。リージョンを
# 変えるならこのファイルも差し替える。
#   取得元 https://truststore.pki.rds.amazonaws.com/ap-northeast-1/ap-northeast-1-bundle.pem
#
# docker の build context が backend / frontend に分かれるため frontend にも同じ
# 内容を置いている。一致は test_db_ssl の TestVerifyFullTrustAnchors が固定する。
_RDS_CA_BUNDLE = Path(__file__).parent / "rds-ca-ap-northeast-1.pem"

# libpq 互換の sslmode allowlist。allowlist 外 (typo) は ValueError で弾く。
_VALID_SSLMODES = frozenset(
    {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
)

# URL から取り除く ssl 系 query param。ひとつでも残ると asyncpg.connect が
# TypeError を出すため、SQLAlchemy URL から network 接続前に剥がす。
_SSL_QUERY_PARAMS = (
    "sslmode",
    "ssl",
    "sslrootcert",
    "sslcert",
    "sslkey",
    "sslcrl",
)
_SSL_QUERY_PARAM_SET = frozenset(_SSL_QUERY_PARAMS)

# [P1] sslmode は signal そのものなので guard から除外する。
_SSL_PARAMS_GUARD_EXEMPT = frozenset({"sslmode"})

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


def _verify_full_context() -> ssl.SSLContext:
    """verify-full (CA + ホスト名検証) の ``SSLContext`` を都度生成する。

    ``create_default_context`` は ``cafile`` を渡すとそのファイルだけを読み、
    ``CERT_REQUIRED`` + ``check_hostname=True`` を既定で有効にする。
    RDS の CA はリージョンの全利用者に証明書を出すため、本人確認はホスト名の
    検証が担う。**module キャッシュしない**: fork する worker で親→子に OpenSSL
    state を共有させないため (engine は WORKER_STARTUP = fork 後生成だが、
    module-level singleton を避けて親で先に作られる経路自体を排除する)。
    engine 生成はプロセス毎に数回のみで CA 読み込みコストは無視できる。
    """
    return ssl.create_default_context(cafile=_RDS_CA_BUNDLE)
