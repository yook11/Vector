"""RDS IAM 認証 provider の不変条件。

token 生成は SigV4 のローカル署名で完結するため、ここでのテストは AWS に出ない
(dummy credential で署名だけ行う)。region は settings から明示的に渡す契約なので、
env に置かない (botocore が読む env 名と本番が注入する env 名が違う)。
"""

from __future__ import annotations

import urllib.parse
from typing import Any

import pytest

import app.db.engine as db_engine
from app.config import settings
from app.db.engine import create_api_engine
from app.db.iam import _rds_client, build_iam_password_provider

_REGION = "ap-northeast-1"
_RDS_URL = (
    "postgresql+asyncpg://vector_auth@"
    "vector-db.abc.ap-northeast-1.rds.amazonaws.com:5432/vector?sslmode=require"
)
_ENDPOINT = "vector-db.abc.ap-northeast-1.rds.amazonaws.com:5432/"


@pytest.fixture(autouse=True)
def isolated_rds_client() -> None:
    """プロセス共有 client の cache を test 間で持ち越さない。"""
    _rds_client.cache_clear()


@pytest.fixture
def aws_signing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """署名に要る credential。ローカル署名のみで実 AWS には出ない。

    本番の credential は ECS の task role (credential endpoint) から来るので、
    ここで env を使うのは署名を成立させるためだけ。region は env に置かない。
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secretexample")


def _token_params(token: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(token.partition("?")[2]))


@pytest.mark.usefixtures("aws_signing_credentials")
class TestBuildIamPasswordProvider:
    @pytest.mark.asyncio
    async def test_token_targets_url_endpoint(self) -> None:
        token = await build_iam_password_provider(_RDS_URL, region=_REGION)()
        assert token.startswith(_ENDPOINT)

    @pytest.mark.asyncio
    async def test_token_names_the_url_user(self) -> None:
        token = await build_iam_password_provider(_RDS_URL, region=_REGION)()
        assert _token_params(token)["DBUser"] == "vector_auth"

    @pytest.mark.asyncio
    async def test_token_is_signed_for_the_given_region(self) -> None:
        """region は settings 由来。botocore の env 解決には委ねない。"""
        token = await build_iam_password_provider(_RDS_URL, region=_REGION)()
        assert f"/{_REGION}/rds-db/" in _token_params(token)["X-Amz-Credential"]

    @pytest.mark.asyncio
    async def test_token_is_short_lived(self) -> None:
        """15 分で失効する。engine 生成時に 1 度作るのでは足りない根拠。"""
        token = await build_iam_password_provider(_RDS_URL, region=_REGION)()
        assert _token_params(token)["X-Amz-Expires"] == "900"

    @pytest.mark.asyncio
    async def test_port_defaults_to_postgres(self) -> None:
        provide = build_iam_password_provider(
            "postgresql+asyncpg://vector_auth@"
            "vector-db.abc.ap-northeast-1.rds.amazonaws.com/vector",
            region=_REGION,
        )
        assert (await provide()).startswith(_ENDPOINT)

    @pytest.mark.asyncio
    async def test_token_port_can_differ_from_connection_port(self) -> None:
        provide = build_iam_password_provider(
            _RDS_URL.replace(":5432/", ":15432/"),
            region=_REGION,
            token_port=5432,
        )
        assert (await provide()).startswith(_ENDPOINT)

    def test_url_without_user_is_rejected(self) -> None:
        """token は user 単位で署名するので、誰として繋ぐか不明なら作れない。"""
        with pytest.raises(ValueError, match="user"):
            build_iam_password_provider(
                "postgresql+asyncpg://vector-db.abc.ap-northeast-1.rds.amazonaws.com/db",
                region=_REGION,
            )

    def test_url_without_host_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="host"):
            build_iam_password_provider(
                "postgresql+asyncpg://vector_auth@/vector", region=_REGION
            )

    def test_client_is_shared_within_the_process(self) -> None:
        """engine ごとに client を作ると worker-analysis で 50MB 超になる (実測)。"""
        assert _rds_client(_REGION) is _rds_client(_REGION)


class TestCreateApiEngine:
    """settings を見る入口。無効なら URL の password をそのまま使う。"""

    @staticmethod
    def _captured_connect_args(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        captured: dict[str, Any] = {}
        real = db_engine.create_async_engine

        def _spy(clean_url: str, **kw: Any) -> Any:
            captured.update(kw)
            return real(clean_url, **kw)

        monkeypatch.setattr(db_engine, "create_async_engine", _spy)
        monkeypatch.setattr(settings, "database_url", _RDS_URL)
        create_api_engine(settings)
        return captured["connect_args"]

    def test_disabled_leaves_password_to_the_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """既定は無効。Fly では経路が一切変わらない。"""
        monkeypatch.setattr(settings, "db_iam_auth", False)
        assert "password" not in self._captured_connect_args(monkeypatch)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("aws_signing_credentials")
    async def test_enabled_injects_token_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "db_iam_auth", True)
        monkeypatch.setattr(settings, "aws_region", _REGION)
        provide = self._captured_connect_args(monkeypatch)["password"]
        assert _token_params(await provide())["DBUser"] == "vector_auth"

    def test_enabled_without_region_fails_loudly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """config validator を迂回して到達した場合も黙って password 認証に落ちない。"""
        monkeypatch.setattr(settings, "db_iam_auth", True)
        monkeypatch.setattr(settings, "aws_region", None)
        monkeypatch.setattr(settings, "database_url", _RDS_URL)
        with pytest.raises(RuntimeError, match="AWS_REGION"):
            create_api_engine(settings)
