"""``acquire_source`` task の例外パス監査テスト + pipeline_stage span 配線テスト。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from logfire.testing import CaptureLogfire
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.domain.event import Stage
from app.collection.article_acquisition.errors import (
    AcquisitionReadError,
)
from app.collection.external_fetch_errors import FetchSsrfBlockedError
from app.models.news_source import NewsSource, SourceType
from app.models.pipeline_event import PipelineEvent
from app.queue.messages.collection import AcquireSourceTaskInput
from app.queue.tasks import acquisition as collection_tasks
from tests.logfire._span_helpers import pipeline_stage_attrs


@pytest.fixture
async def vb_source(db_session: AsyncSession) -> NewsSource:
    source = NewsSource(
        name="VentureBeat",
        source_type=SourceType.RSS,
        site_url="https://venturebeat.com",
        endpoint_url="https://venturebeat.com/feed/",
        is_active=True,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    return source


def _ctx(session_factory: async_sessionmaker[AsyncSession]) -> SimpleNamespace:
    """taskiq Context の最低限な mock。"""
    state = SimpleNamespace(session_factory=session_factory)
    message = SimpleNamespace(labels={})
    return SimpleNamespace(state=state, message=message)


class _RaisingService:
    """指定された例外を raise する ArticleAcquisitionService スタンド。"""

    def __init__(self, *_: Any, **__: Any) -> None: ...

    async def execute(self, source_id: int) -> Any:
        raise self.exc  # type: ignore[attr-defined]


def _patch_service_to_raise(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    cls = type(
        "_S",
        (_RaisingService,),
        {"exc": exc},
    )
    monkeypatch.setattr(
        "app.collection.article_acquisition.service.ArticleAcquisitionService",
        cls,
    )


async def _failed_event(db_session: AsyncSession) -> PipelineEvent:
    return (
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.event_type == "failed")
            )
        )
        .scalars()
        .one()
    )


@pytest.mark.asyncio
async def test_acquisition_error_records_origin_code_and_returns(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage 1 marker → audit + error dict を return。"""
    _patch_service_to_raise(
        monkeypatch,
        AcquisitionReadError(origin=FetchSsrfBlockedError("ssrf blocked: 10.0.0.1")),
    )
    ctx = _ctx(session_factory)

    result = await collection_tasks.acquire_source(
        AcquireSourceTaskInput(id=vb_source.id, name=str(vb_source.name)),
        ctx=ctx,  # type: ignore[arg-type]
    )

    assert result["status"] == "error"
    row = await _failed_event(db_session)
    assert row.outcome_code == "fetch_ssrf_blocked"
    assert row.retryability == "non_retryable"
    assert "code" not in row.payload
    assert row.source_id == vb_source.id
    assert row.error_class.endswith(  # type: ignore[union-attr]
        ".AcquisitionReadError"
    )
    assert row.payload["failure_kind"] == "external_fetch"
    assert row.payload["failure_action"] is None


class TestAcquireSourceStageSpan:
    """``acquire_source`` task が pipeline_stage span を正しく開く配線テスト。

    task body 内の lazy import パスを patch して実 DB / 実ネットワークを回避する。
    """

    @pytest.mark.asyncio
    async def test_span_stage_op_and_source_id(self, capfire: CaptureLogfire) -> None:
        """正常系: stage=acquisition / op=acquire_source / source_id が span に開く。"""
        target_source_id = 7

        ctx = MagicMock()
        ctx.state.session_factory = MagicMock()
        ctx.message.labels = {}

        # task body 内の lazy import パスを patch する。
        # SOURCES は dict-like に振る舞う必要があるため __getitem__ を設定。
        fake_sources = MagicMock()
        fake_sources.__getitem__ = MagicMock(return_value=MagicMock())

        with (
            patch(
                "app.collection.article_acquisition.service.ArticleAcquisitionService",
                return_value=MagicMock(execute=AsyncMock(return_value=[])),
            ),
            patch(
                "app.collection.article_acquisition.strategy.SOURCES",
                fake_sources,
            ),
            patch(
                "app.collection.sources.source_name.SourceName",
                return_value=MagicMock(),
            ),
            patch(
                "app.queue.tasks.acquisition.record_acquisition_run",
            ),
            patch(
                "app.queue.tasks.acquisition.ArticleAcquisitionFailureHandler",
                return_value=MagicMock(
                    handle_source_failure=AsyncMock(return_value=False)
                ),
            ),
            patch(
                "app.queue.tasks.acquisition.curate_content",
                new=MagicMock(kiq=AsyncMock()),
            ),
        ):
            await collection_tasks.acquire_source(
                AcquireSourceTaskInput(id=target_source_id, name="FakeSource"),
                ctx=ctx,  # type: ignore[arg-type]
            )

        attrs = pipeline_stage_attrs(capfire)
        assert attrs["stage"] == Stage.ACQUISITION.value  # == "acquisition"
        assert attrs["op"] == "acquire_source"
        assert attrs["source_id"] == target_source_id


@pytest.mark.asyncio
async def test_unexpected_error_records_then_reraises(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    vb_source: NewsSource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage 1 marker 外の Exception は audit + re-raise する。"""
    _patch_service_to_raise(monkeypatch, RuntimeError("boom"))
    ctx = _ctx(session_factory)

    with pytest.raises(RuntimeError, match="boom"):
        await collection_tasks.acquire_source(
            AcquireSourceTaskInput(id=vb_source.id, name=str(vb_source.name)),
            ctx=ctx,  # type: ignore[arg-type]
        )

    row = await _failed_event(db_session)
    assert row.outcome_code == "unexpected_error"
    assert row.retryability == "unknown"
    assert row.error_class.endswith(".RuntimeError")  # type: ignore[union-attr]
    assert row.payload["failure_kind"] == "unknown"
    assert row.payload["failure_action"] is None
