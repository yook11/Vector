"""RDS IAM 認証 provider の不変条件。

token 生成は SigV4 のローカル署名で完結するため、ここでのテストは AWS に出ない
(dummy credential で署名だけ行う)。
"""

from __future__ import annotations

import urllib.parse
from typing import Any

import pytest

import app.db_ssl as db_ssl
from app.config import settings
from app.db_iam_auth import build_iam_password_provider, create_runtime_engine

_RDS_URL = (
    "postgresql+asyncpg://vector_app@"
    "vector-db.abc.ap-northeast-1.rds.amazonaws.com:5432/vector?sslmode=require"
)
_ENDPOINT = "vector-db.abc.ap-northeast-1.rds.amazonaws.com:5432/"


@pytest.fixture
def aws_signing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """署名に要る credential と region。ローカル署名のみで実 AWS には出ない。"""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secretexample")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")


def _token_params(token: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(token.partition("?")[2]))


@pytest.mark.usefixtures("aws_signing_env")
class TestBuildIamPasswordProvider:
    @pytest.mark.asyncio
    async def test_token_targets_url_endpoint(self) -> None:
        token = await build_iam_password_provider(_RDS_URL)()
        assert token.startswith(_ENDPOINT)

    @pytest.mark.asyncio
    async def test_token_names_the_url_user(self) -> None:
        token = await build_iam_password_provider(_RDS_URL)()
        assert _token_params(token)["DBUser"] == "vector_app"

    @pytest.mark.asyncio
    async def test_token_is_short_lived(self) -> None:
        """15 分で失効する。engine 生成時に 1 度作るのでは足りない根拠。"""
        token = await build_iam_password_provider(_RDS_URL)()
        assert _token_params(token)["X-Amz-Expires"] == "900"

    @pytest.mark.asyncio
    async def test_port_defaults_to_postgres(self) -> None:
        provide = build_iam_password_provider(
            "postgresql+asyncpg://vector_app@"
            "vector-db.abc.ap-northeast-1.rds.amazonaws.com/vector"
        )
        assert (await provide()).startswith(_ENDPOINT)

    def test_url_without_user_is_rejected(self) -> None:
        """token は user 単位で署名するので、誰として繋ぐか不明なら作れない。"""
        with pytest.raises(ValueError, match="user"):
            build_iam_password_provider(
                "postgresql+asyncpg://vector-db.abc.ap-northeast-1.rds.amazonaws.com/db"
            )

    def test_url_without_host_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="host"):
            build_iam_password_provider("postgresql+asyncpg://vector_app@/vector")


class TestCreateRuntimeEngine:
    """settings を見る入口。無効なら URL の password をそのまま使う。"""

    @staticmethod
    def _captured_connect_args(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        captured: dict[str, Any] = {}
        real = db_ssl.create_async_engine

        def _spy(clean_url: str, **kw: Any) -> Any:
            captured.update(kw)
            return real(clean_url, **kw)

        monkeypatch.setattr(db_ssl, "create_async_engine", _spy)
        create_runtime_engine(_RDS_URL)
        return captured["connect_args"]

    def test_disabled_leaves_password_to_the_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """既定は無効。Fly では経路が一切変わらない。"""
        monkeypatch.setattr(settings, "db_iam_auth", False)
        assert "password" not in self._captured_connect_args(monkeypatch)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("aws_signing_env")
    async def test_enabled_injects_token_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "db_iam_auth", True)
        provide = self._captured_connect_args(monkeypatch)["password"]
        assert _token_params(await provide())["DBUser"] == "vector_app"
