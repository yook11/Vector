"""共有接続管理の取得・解放・失敗の優先順位を確認する。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.db import engine as engine_module
from app.lambda_handlers.backfill import execution, resources
from app.lambda_handlers.backfill.settings import CurationBackfillSettings
from app.outbox.publishing.analyzable_created import build_analyzable_created_message
from app.outbox.publishing.route import EventDeliveryRoute

pytestmark = pytest.mark.unit


@pytest.fixture
def wiring(monkeypatch):
    settings = CurationBackfillSettings(
        env="test",
        database_url="postgresql+asyncpg://user@database.invalid/db",
        aws_region="ap-northeast-1",
        sqs_article_curation_queue_url="https://sqs.invalid/curation",
    )
    route = EventDeliveryRoute(
        event_type="article.analyzable_created",
        queue_url=settings.sqs_article_curation_queue_url,
        build_message=build_analyzable_created_message,
    )
    closed = []
    rds = Mock(close=Mock(side_effect=lambda: closed.append("rds")))
    engine = Mock(dispose=AsyncMock(side_effect=lambda: closed.append("engine")))
    session = Mock(create_client=Mock(return_value=rds))
    monkeypatch.setattr(resources, "Session", Mock(return_value=session))
    create_engine = Mock(return_value=engine)
    monkeypatch.setattr(resources, "create_backfill_engine", create_engine)
    factory = object()
    monkeypatch.setattr(
        resources, "caller_managed_session_factory", Mock(return_value=factory)
    )
    sender = Mock()
    monkeypatch.setattr(resources.SqsSender, "from_session", Mock(return_value=sender))
    return SimpleNamespace(
        settings=settings,
        route=route,
        rds=rds,
        engine=engine,
        factory=factory,
        closed=closed,
        create_engine=create_engine,
        session=session,
    )


@pytest.mark.asyncio
async def test_resources_are_borrowed_and_closed_in_reverse_order(wiring):
    """呼び出し内で準備した資源を、終了時に逆順で解放する。"""
    async with resources.open_backfill_resources(
        wiring.settings, stage="curation", route=wiring.route
    ) as opened:
        assert opened.session_factory is wiring.factory
        assert wiring.closed == []
    assert wiring.closed == ["engine", "rds"]
    assert wiring.create_engine.call_args.kwargs["stage"] == "curation"
    config = wiring.session.create_client.call_args.kwargs["config"]
    assert config.proxies == {}
    assert config.ignore_configured_endpoint_urls is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase,expected",
    [("rds", []), ("engine", ["rds"]), ("publisher", ["engine", "rds"])],
)
async def test_initialization_failure_releases_acquired_resources(
    wiring, monkeypatch, phase, expected
):
    """初期化途中で失敗しても取得済みの資源だけを解放する。"""
    error = RuntimeError(phase)
    if phase == "rds":
        wiring.session.create_client.side_effect = error
    elif phase == "engine":
        wiring.create_engine.side_effect = error
    else:
        monkeypatch.setattr(
            resources.SqsSender, "from_session", Mock(side_effect=error)
        )
    with pytest.raises(RuntimeError) as caught:
        async with resources.open_backfill_resources(
            wiring.settings, stage="curation", route=wiring.route
        ):
            pytest.fail("must not yield")
    assert caught.value is error
    assert wiring.closed == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("original", [RuntimeError("work"), asyncio.CancelledError()])
async def test_cleanup_failures_preserve_original_failure(wiring, original):
    """終了も失敗した場合に実行の元の例外・キャンセルを保全する。"""
    wiring.engine.dispose.side_effect = ValueError("dispose")
    wiring.rds.close.side_effect = ValueError("close")
    with pytest.raises(type(original)) as caught:
        async with resources.open_backfill_resources(
            wiring.settings, stage="curation", route=wiring.route
        ):
            raise original
    assert caught.value is original
    wiring.engine.dispose.assert_awaited_once()
    wiring.rds.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("dispose"), asyncio.CancelledError()])
async def test_cleanup_only_failure_propagates_and_closes_rds(wiring, failure):
    """終了単独の失敗・キャンセルは残りを解放してから伝える。"""
    wiring.engine.dispose.side_effect = failure
    with pytest.raises(type(failure)) as caught:
        async with resources.open_backfill_resources(
            wiring.settings, stage="curation", route=wiring.route
        ):
            pass
    assert caught.value is failure
    wiring.rds.close.assert_called_once()


@pytest.mark.asyncio
async def test_rds_cleanup_only_failure_is_not_success(wiring):
    """RDSクライアントの終了失敗も正常終了にしない。"""
    error = RuntimeError("close")
    wiring.rds.close.side_effect = error
    with pytest.raises(RuntimeError) as caught:
        async with resources.open_backfill_resources(
            wiring.settings, stage="curation", route=wiring.route
        ):
            pass
    assert caught.value is error


@pytest.mark.asyncio
async def test_shared_execution_calls_operation_once_and_releases_on_failure(wiring):
    """共有実行が借用資源で一度呼び、失敗を解放後に伝える。"""
    from datetime import UTC, datetime

    now = datetime(2026, 9, 14, tzinfo=UTC)
    error = RuntimeError("operation")
    operation = AsyncMock(side_effect=error)
    with pytest.raises(RuntimeError) as caught:
        await execution.run_backfill(
            wiring.settings,
            stage="curation",
            route=wiring.route,
            operation=operation,
            now=now,
        )
    assert caught.value is error
    operation.assert_awaited_once()
    assert operation.call_args.kwargs["session_factory"] is wiring.factory
    assert operation.call_args.kwargs["now"] is now
    assert operation.call_args.kwargs["enabled"] is True
    assert wiring.closed == ["engine", "rds"]


@pytest.mark.parametrize("stage", ["curation", "assessment", "embedding"])
def test_engine_uses_stage_identity_and_one_connection(wiring, monkeypatch, stage):
    """工程別識別名と単一接続・既存タイムアウトをDB生成へ渡す。"""
    create = Mock()
    monkeypatch.setattr(engine_module, "_create_engine", create)
    provider = AsyncMock(return_value="token")
    engine_module.create_backfill_engine(
        wiring.settings, stage=stage, password_provider=provider
    )
    create.assert_called_once_with(
        wiring.settings.database_url,
        application_name=f"vector-backfill-{stage}",
        password_provider=provider,
        pool_size=1,
        max_overflow=0,
        pool_timeout=5,
        connect_args={"timeout": 5, "command_timeout": 5},
        echo=False,
    )


@pytest.mark.parametrize(
    "override", [{"db_iam_auth": False}, {"password_provider": None}]
)
def test_engine_rejects_missing_iam_requirements(wiring, override):
    """engine生成でもIAM設定と署名providerを必須にする。"""
    provider = override.get("password_provider", AsyncMock())
    settings = wiring.settings.model_copy(
        update={"db_iam_auth": override.get("db_iam_auth", True)}
    )
    with pytest.raises((ValueError, TypeError)):
        engine_module.create_backfill_engine(
            settings, stage="curation", password_provider=provider
        )
