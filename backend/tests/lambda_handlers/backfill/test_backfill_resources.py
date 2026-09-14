"""DB不要のengine生成設定と認証の前提だけを確認する。"""

from unittest.mock import AsyncMock, Mock

import pytest

from app.db import engine as engine_module
from app.lambda_handlers.backfill.settings import CurationBackfillSettings

pytestmark = pytest.mark.unit


@pytest.fixture
def settings():
    return CurationBackfillSettings(
        env="test",
        database_url="postgresql+asyncpg://user@database.invalid/db",
        aws_region="ap-northeast-1",
        sqs_article_curation_queue_url="https://sqs.invalid/curation",
    )


@pytest.mark.parametrize(
    "stage,application_name",
    [
        ("curation", "vector-backfill-curation"),
        ("assessment", "vector-backfill-assessment"),
        ("embedding", "vector-backfill-embedding"),
    ],
)
def test_engine_identifies_backfill_stage(
    settings, monkeypatch, stage, application_name
):
    """DB接続の識別名に実行する工程が反映される。"""
    create = Mock()
    monkeypatch.setattr(engine_module, "_create_engine", create)
    engine_module.create_backfill_engine(
        settings, stage=stage, password_provider=AsyncMock()
    )
    assert create.call_args.kwargs["application_name"] == application_name


def test_engine_connection_limits_match_consumer_defaults(settings, monkeypatch):
    """単一接続と既存の5秒上限をengine生成へ渡す。"""
    create = Mock()
    monkeypatch.setattr(engine_module, "_create_engine", create)
    provider = AsyncMock()
    engine_module.create_backfill_engine(
        settings, stage="curation", password_provider=provider
    )
    create.assert_called_once_with(
        settings.database_url,
        application_name="vector-backfill-curation",
        password_provider=provider,
        pool_size=1,
        max_overflow=0,
        pool_timeout=5,
        connect_args={"timeout": 5, "command_timeout": 5},
        echo=False,
    )


def test_engine_rejects_disabled_iam(settings):
    """IAM無効化はengine生成時にも拒否する。"""
    with pytest.raises(ValueError, match="requires RDS IAM"):
        engine_module.create_backfill_engine(
            settings.model_copy(update={"db_iam_auth": False}),
            stage="curation",
            password_provider=AsyncMock(),
        )


def test_engine_requires_password_provider(settings):
    """署名providerがない場合は型エラーとして拒否する。"""
    with pytest.raises(TypeError, match="IAM password provider"):
        engine_module.create_backfill_engine(
            settings, stage="curation", password_provider=None
        )


@pytest.mark.asyncio
async def test_rds_signer_uses_region_without_external_endpoint_settings(
    settings, monkeypatch
):
    """署名器のSDK設定はリージョンを固定し、プロキシと外部endpoint設定を使わない。"""
    from app.lambda_handlers.backfill import resources
    from app.outbox.publishing.analyzable_created import (
        build_analyzable_created_message,
    )
    from app.outbox.publishing.route import EventDeliveryRoute

    stop = RuntimeError("stop before connection creation")
    client = Mock(side_effect=stop)
    monkeypatch.setattr(resources, "Session", lambda: Mock(create_client=client))
    route = EventDeliveryRoute(
        "article.analyzable_created",
        settings.sqs_article_curation_queue_url,
        build_analyzable_created_message,
    )
    with pytest.raises(RuntimeError):
        async with resources.open_backfill_resources(
            settings, stage="curation", route=route
        ):
            pytest.fail("must not acquire resources")
    assert client.call_args.args == ("rds",)
    assert client.call_args.kwargs["region_name"] == settings.aws_region
    config = client.call_args.kwargs["config"]
    assert config.proxies == {}
    assert config.ignore_configured_endpoint_urls is True
