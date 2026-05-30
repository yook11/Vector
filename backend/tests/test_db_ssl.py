"""``app.db_ssl`` の純粋ヘルパー + engine factory の不変条件テスト。

frontend の ``frontend/src/lib/auth/pool-ssl.test.ts`` 5 ケースを backend に
移植し、backend 固有 (verify-full SSLContext / asyncpg 非対応 param 除去 /
平文化拒否 / factory の SSL 一元化) を足す。純粋関数を公開 signature で直接呼ぶ。
"""

from __future__ import annotations

import ssl
from typing import Any

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine

import app.db_ssl as db_ssl
from app.db_ssl import (
    create_app_engine,
    parse_sslmode,
    split_ssl_from_url,
)

# verify-full で繋ぐ Neon 風 URL (実接続はしない)。
_NEON = "postgresql+asyncpg://vector_app:strongpass@ep-x.ap-southeast-1.aws.neon.tech/neondb"
# sslmode を持たない dev (docker) 風 URL。
_DEV = "postgresql+asyncpg://vector_app:strongpass@db:5432/vector"

# split_ssl_from_url が URL から剥がす ssl 系 param (production 由来の表面)。
_STRIPPED_PARAMS = (
    "sslmode",
    "channel_binding",
    "ssl",
    "sslrootcert",
    "sslcert",
    "sslkey",
    "sslcrl",
)


class TestSplitSslFromUrl:
    """接続文字列 → (clean_url, connect_args) 分解の不変条件。"""

    def test_sslmode_require_enables_ssl_and_strips_param(self) -> None:
        # sslmode=require → connect_args に ssl、clean_url から sslmode 消失
        clean_url, connect_args = split_ssl_from_url(f"{_NEON}?sslmode=require")
        assert "ssl" in connect_args
        assert "sslmode" not in make_url(clean_url).query

    def test_channel_binding_is_stripped(self) -> None:
        # Neon ネイティブ文字列の channel_binding は asyncpg 非対応なので除去
        clean_url, _ = split_ssl_from_url(
            f"{_NEON}?sslmode=require&channel_binding=require"
        )
        assert "channel_binding" not in make_url(clean_url).query

    def test_no_sslmode_disables_ssl(self) -> None:
        # sslmode 無し (dev / docker) は SSL 無効 = connect_args 空
        _, connect_args = split_ssl_from_url(_DEV)
        assert connect_args == {}

    def test_sslmode_disable_disables_ssl(self) -> None:
        # sslmode=disable は明示的に SSL を無効化する
        _, connect_args = split_ssl_from_url(f"{_DEV}?sslmode=disable")
        assert connect_args == {}

    def test_non_ssl_query_is_preserved(self) -> None:
        # ssl 系以外の query (中立 param) は clean_url に保持する
        clean_url, _ = split_ssl_from_url(f"{_DEV}?foo=bar")
        assert make_url(clean_url).query.get("foo") == "bar"

    def test_ssl_context_is_verify_full(self) -> None:
        # SSL 有効時の SSLContext は verify-full (CA + ホスト名検証) 相当
        _, connect_args = split_ssl_from_url(f"{_NEON}?sslmode=require")
        ctx = connect_args["ssl"]
        assert isinstance(ctx, ssl.SSLContext)
        assert ctx.check_hostname is True
        assert ctx.verify_mode == ssl.CERT_REQUIRED

    def test_scheme_is_preserved(self) -> None:
        # postgresql+asyncpg:// prefix を壊さない (dialect+driver 維持)
        clean_url, _ = split_ssl_from_url(f"{_NEON}?sslmode=require")
        assert clean_url.startswith("postgresql+asyncpg://")

    @pytest.mark.parametrize(
        "param_kv",
        [
            "channel_binding=require",
            "ssl=true",
            "sslrootcert=/etc/ca.pem",
            "sslcert=/etc/client.crt",
            "sslkey=/etc/client.key",
            "sslcrl=/etc/crl.pem",
        ],
    )
    def test_ssl_family_params_are_stripped(self, param_kv: str) -> None:
        # sslmode と併記された各 ssl 系 param が入力に在り、出力で消えること
        # (非空虚: 入力に在ることも assert)。
        key = param_kv.split("=", 1)[0]
        raw = f"{_NEON}?sslmode=require&{param_kv}"
        assert key in make_url(raw).query  # 入力に在る
        clean_url, _ = split_ssl_from_url(raw)
        assert key not in make_url(clean_url).query  # 出力で消える

    @pytest.mark.parametrize(
        "bad_url",
        [
            f"{_NEON}?ssl=verify-full",
            f"{_NEON}?sslrootcert=/etc/ca.pem",
            f"{_NEON}?sslcert=/etc/client.crt",
        ],
    )
    def test_ssl_param_without_sslmode_raises(self, bad_url: str) -> None:
        # [P1] sslmode 抜きで ssl 系のみ指定は黙って平文化せず ValueError
        # (asyncpg は raw ssl param を無視して平文接続してしまうため)。
        with pytest.raises(ValueError, match="sslmode"):
            split_ssl_from_url(bad_url)

    def test_sslmode_typo_raises(self) -> None:
        # [P1] allowlist 外 (typo) は ValueError で見逃さない
        with pytest.raises(ValueError, match="invalid sslmode"):
            split_ssl_from_url(f"{_NEON}?sslmode=requrie")

    def test_uppercase_ssl_param_is_handled(self) -> None:
        # 大文字変種 (?SSLMODE=require) も SSL 有効化し、clean_url から除去する
        # (取りこぼすと asyncpg.connect に大文字 param が残り TypeError になる)。
        clean_url, connect_args = split_ssl_from_url(f"{_NEON}?SSLMODE=require")
        assert "ssl" in connect_args
        assert "SSLMODE" not in make_url(clean_url).query

    def test_clean_is_not_ssl_idempotent(self) -> None:
        # 冪等性: clean 済み URL を再投入すると clean_url 不変。ただし SSL シグナルは
        # sslmode に在ったため、二度目は sslmode が無く connect_args 空になる
        # (= clean と SSL 導出は同一 call で行う必要がある: create_app_engine の根拠)。
        clean_url, connect_args = split_ssl_from_url(f"{_NEON}?sslmode=require")
        assert connect_args != {}
        clean_url2, connect_args2 = split_ssl_from_url(clean_url)
        assert clean_url2 == clean_url
        assert connect_args2 == {}

    def test_credentials_are_preserved(self) -> None:
        # user / password / host / port / dbname が clean_url に保持される
        raw = "postgresql+asyncpg://u:p@h:6543/dbn?sslmode=require"
        clean_url, _ = split_ssl_from_url(raw)
        url = make_url(clean_url)
        assert (url.username, url.password, url.host, url.port, url.database) == (
            "u",
            "p",
            "h",
            6543,
            "dbn",
        )


class TestParseSslmode:
    """sslmode 抽出 + allowlist 検証 (config validator と factory の SSoT)。"""

    def test_returns_value_when_present(self) -> None:
        assert parse_sslmode(f"{_NEON}?sslmode=require") == "require"

    def test_returns_none_when_absent(self) -> None:
        assert parse_sslmode(_DEV) is None

    def test_returns_disable_verbatim(self) -> None:
        assert parse_sslmode(f"{_DEV}?sslmode=disable") == "disable"

    def test_typo_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid sslmode"):
            parse_sslmode(f"{_NEON}?sslmode=requrie")

    def test_uppercase_key_and_value_normalized(self) -> None:
        # 大文字変種 (?SSLMODE=REQUIRE) も取りこぼさず小文字に正規化する
        # (取りこぼすと sslmode 無し扱い = 平文降格になる)。
        assert parse_sslmode(f"{_NEON}?SSLMODE=REQUIRE") == "require"

    def test_duplicate_same_key_raises(self) -> None:
        # 同一 key の複数指定 (require & disable) は曖昧なので「高々 1 回」違反
        with pytest.raises(ValueError, match="at most once"):
            parse_sslmode(f"{_NEON}?sslmode=require&sslmode=disable")

    def test_mixed_case_duplicate_raises(self) -> None:
        # 大小違いの重複 (sslmode & SSLMODE) も「高々 1 回」違反として弾く
        with pytest.raises(ValueError, match="at most once"):
            parse_sslmode(f"{_NEON}?sslmode=require&SSLMODE=disable")


class TestCreateAppEngine:
    """SSL 一元注入 factory (engine 構築のみ、実接続なし)。"""

    def test_engine_url_has_no_ssl_params(self) -> None:
        # sslmode=require から作った engine の url に ssl 系 param が残らない
        engine = create_app_engine(f"{_NEON}?sslmode=require")
        query = engine.url.query
        assert all(p not in query for p in _STRIPPED_PARAMS)

    def test_engine_kwargs_propagate(self) -> None:
        # **kw (echo 等) が engine に伝播する
        engine = create_app_engine(f"{_NEON}?sslmode=require", echo=True)
        assert engine.sync_engine.echo is True

    def test_caller_passed_ssl_connect_arg_raises(self) -> None:
        # SSL 決定権の一元化: 呼び出し側の connect_args['ssl'] は fail-fast
        ctx = ssl.create_default_context()
        with pytest.raises(ValueError, match="connect_args"):
            create_app_engine(f"{_NEON}?sslmode=require", connect_args={"ssl": ctx})

    def test_non_ssl_connect_args_are_merged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 呼び出し側の非 ssl connect_args (例 command_timeout) は SSL 注入後も
        # 保持される。external sink (create_async_engine) に渡る dict を捕捉して
        # 注入された ssl と caller の key が共存することを検証する。
        captured: dict[str, Any] = {}

        def _spy(clean_url: str, **kw: Any) -> Any:
            captured.update(kw)
            return _real_create_async_engine(clean_url, **kw)

        monkeypatch.setattr(db_ssl, "create_async_engine", _spy)
        create_app_engine(
            f"{_NEON}?sslmode=require", connect_args={"command_timeout": 30}
        )
        assert captured["connect_args"]["command_timeout"] == 30
        assert "ssl" in captured["connect_args"]
