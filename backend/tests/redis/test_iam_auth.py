"""ElastiCache IAM 認証 provider の不変条件。

token 生成は SigV4 のローカル署名で完結するため、ここでのテストは AWS に出ない
(dummy credential で署名だけ行う)。region / cache 名は settings から明示的に渡す
契約なので、env には置かない (``app/db_iam_auth.py`` のテストと同じ判断)。
"""

from __future__ import annotations

import urllib.parse

import pytest

from app.config import settings
from app.redis.iam_auth import (
    ElastiCacheIAMProvider,
    _request_signer,
    redis_connection_options,
)

_REGION = "ap-northeast-1"
_CACHE_NAME = "vector-cache-abc123"
_USER = "vector-app"
_IAM_URL = f"redis://{_USER}@vector-cache.abc.cache.amazonaws.com:6379/3"


@pytest.fixture(autouse=True)
def isolated_request_signer() -> None:
    """プロセス共有 signer の cache を test 間で持ち越さない。"""
    _request_signer.cache_clear()


@pytest.fixture
def aws_signing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """署名に要る credential。ローカル署名のみで実 AWS には出ない。"""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secretexample")


def _token_params(token: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(token.partition("?")[2]))


def _provider() -> ElastiCacheIAMProvider:
    return ElastiCacheIAMProvider(user=_USER, cache_name=_CACHE_NAME, region=_REGION)


@pytest.mark.usefixtures("aws_signing_credentials")
class TestElastiCacheIAMProvider:
    def test_credentials_username_is_the_connect_user(self) -> None:
        user, _token = _provider().get_credentials()
        assert user == _USER

    def test_token_targets_cache_name_without_scheme(self) -> None:
        """署名の host は cache 名。scheme を含めると AUTH password の形式が壊れる。"""
        _user, token = _provider().get_credentials()
        assert token.startswith(f"{_CACHE_NAME}/?")
        assert "https://" not in token

    def test_token_names_the_connect_action_and_user(self) -> None:
        _user, token = _provider().get_credentials()
        params = _token_params(token)
        assert params["Action"] == "connect"
        assert params["User"] == _USER

    def test_token_is_signed(self) -> None:
        _user, token = _provider().get_credentials()
        assert "X-Amz-Signature" in _token_params(token)

    def test_token_is_short_lived(self) -> None:
        """15 分で失効する。token をキャッシュしない根拠。"""
        _user, token = _provider().get_credentials()
        assert _token_params(token)["X-Amz-Expires"] == "900"

    @pytest.mark.asyncio
    async def test_get_credentials_async_matches_sync_shape(self) -> None:
        """async 経路 (to_thread 経由) も同じ user・action に対する token を作る。

        X-Amz-Date は秒精度で変わりうるため、署名バイト列そのものではなく
        署名対象の構造 (User / Action) が一致することを見る。
        """
        sync_user, sync_token = _provider().get_credentials()
        async_user, async_token = await _provider().get_credentials_async()
        sync_params = _token_params(sync_token)
        async_params = _token_params(async_token)
        assert async_user == sync_user
        assert async_params["Action"] == sync_params["Action"]
        assert async_params["User"] == sync_params["User"]
        assert async_token.startswith(f"{_CACHE_NAME}/?")

    def test_signer_is_shared_within_the_process(self) -> None:
        """DB 側の ``_rds_client`` と同じ判断: 解決を 1 本に共有する。"""
        assert _request_signer(_REGION) is _request_signer(_REGION)


class TestRedisConnectionOptions:
    """settings を見る入口。無効なら URL をそのまま使う。"""

    def test_disabled_leaves_url_and_kwargs_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """既定は無効。Fly / dev では URL も kwargs も一切変わらない。"""
        monkeypatch.setattr(settings, "redis_iam_auth", False)
        password_url = "redis://:secret@localhost:6379/0"
        url, kwargs = redis_connection_options(password_url)
        assert url == password_url
        assert kwargs == {}

    def test_enabled_strips_userinfo_but_keeps_the_rest_of_the_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """username + credential_provider の併用は redis-py が DataError で拒否する。"""
        monkeypatch.setattr(settings, "redis_iam_auth", True)
        monkeypatch.setattr(settings, "aws_region", _REGION)
        monkeypatch.setattr(settings, "redis_iam_cache_name", _CACHE_NAME)
        url, _kwargs = redis_connection_options(_IAM_URL)
        parsed = urllib.parse.urlparse(url)
        assert parsed.username is None
        assert parsed.password is None
        assert parsed.scheme == "redis"
        assert parsed.hostname == "vector-cache.abc.cache.amazonaws.com"
        assert parsed.port == 6379
        assert parsed.path == "/3"

    def test_enabled_provider_is_bound_to_the_url_user(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "redis_iam_auth", True)
        monkeypatch.setattr(settings, "aws_region", _REGION)
        monkeypatch.setattr(settings, "redis_iam_cache_name", _CACHE_NAME)
        _url, kwargs = redis_connection_options(_IAM_URL)
        provider = kwargs["credential_provider"]
        assert isinstance(provider, ElastiCacheIAMProvider)
        assert provider.user == _USER
        assert provider.cache_name == _CACHE_NAME
        assert provider.region == _REGION

    def test_enabled_without_url_user_fails_loudly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """黙って匿名接続にしない。token は user 単位で署名する。"""
        monkeypatch.setattr(settings, "redis_iam_auth", True)
        monkeypatch.setattr(settings, "aws_region", _REGION)
        monkeypatch.setattr(settings, "redis_iam_cache_name", _CACHE_NAME)
        with pytest.raises(ValueError, match="user"):
            redis_connection_options(
                "redis://vector-cache.abc.cache.amazonaws.com:6379/0"
            )

    def test_enabled_without_region_fails_loudly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """config validator を迂回して到達した場合も黙って password 認証に落ちない。"""
        monkeypatch.setattr(settings, "redis_iam_auth", True)
        monkeypatch.setattr(settings, "aws_region", None)
        monkeypatch.setattr(settings, "redis_iam_cache_name", _CACHE_NAME)
        with pytest.raises(RuntimeError, match="AWS_REGION"):
            redis_connection_options(_IAM_URL)

    def test_enabled_without_cache_name_fails_loudly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "redis_iam_auth", True)
        monkeypatch.setattr(settings, "aws_region", _REGION)
        monkeypatch.setattr(settings, "redis_iam_cache_name", None)
        with pytest.raises(RuntimeError, match="REDIS_IAM_CACHE_NAME"):
            redis_connection_options(_IAM_URL)
