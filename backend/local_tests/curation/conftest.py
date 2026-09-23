"""実Curationの接続設定と外部通信境界の差し替えを共有する。"""

from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import SecretStr

from app.ai_providers.gemini import client as gemini_module
from app.lambda_handlers import article_analysis_lifecycle as resource_module
from app.lambda_handlers.curation.settings import CurationConsumerSettings
from local_tests.curation.support import handler_module
from tests.iam_fixtures import inject_test_db_signer


@pytest.fixture
def gemini_response():
    return AsyncMock()


@pytest.fixture
def curation_runtime(system_database, monkeypatch, gemini_response):
    settings = CurationConsumerSettings(
        env="test",
        aws_region="ap-northeast-1",
        database_url=inject_test_db_signer(
            monkeypatch,
            system_database.url("vector_article_analysis", sqlalchemy=True),
            resources_module=resource_module,
        ),
        db_iam_auth=True,
        gemini_api_key_parameter_path="/test/gemini-key",
    )
    monkeypatch.setattr(handler_module, "CurationConsumerSettings", lambda: settings)
    monkeypatch.setattr(
        resource_module,
        "get_secret_parameter",
        Mock(return_value=SecretStr("test-key")),
    )

    def http_factory(**kwargs):
        kwargs.pop("retries")
        return httpx.AsyncClient(  # noqa: TID251
            transport=httpx.MockTransport(gemini_response), **kwargs
        )

    monkeypatch.setattr(gemini_module, "make_external_async_client", http_factory)
