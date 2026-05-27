"""``PipelineEventRepository`` の単体テスト。

- append が ORM を session.add し commit 後に SELECT 可能
- payload Pydantic dump → JSONB → 同値で読み戻し
- source_id 自動補完 (article_id 経由の逆引き)
- StrEnum 値 set が CHECK 制約値と一致 (二重チェック)
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import (
    AcquisitionPayload,
    EmbeddingPayload,
)
from app.audit.failure_projection import Retryability
from app.audit.repository import PipelineEventRepository
from app.models.article import Article as ArticleORM
from app.models.news_source import NewsSource, SourceType
from app.models.pipeline_event import PipelineEvent


@pytest.fixture
async def source_row(db_session: AsyncSession) -> NewsSource:
    src = NewsSource(
        name="VentureBeat",
        source_type=SourceType.RSS,
        site_url="https://venturebeat.com",
        endpoint_url="https://venturebeat.com/feed/",
        is_active=True,
    )
    db_session.add(src)
    await db_session.commit()
    await db_session.refresh(src)
    return src


@pytest.fixture
async def article_row(db_session: AsyncSession, source_row: NewsSource) -> ArticleORM:
    url = "https://venturebeat.com/a/"
    article = ArticleORM(
        source_id=source_row.id,
        source_url=url,  # type: ignore[arg-type]
        original_title="t",
        original_content="c" * 100,
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    return article


@pytest.mark.asyncio
async def test_append_inserts_row_with_payload_roundtrip(
    db_session: AsyncSession, source_row: NewsSource
) -> None:
    """``AcquisitionPayload`` (failure-style snapshot) の DB roundtrip。

    成功側 audit 撤去後、``AcquisitionPayload`` は failure path 専用に
    なったので Task 例外パスでの書込形態 (``event_type=FAILED`` +
    ``outcome_code=permanent_fetch_error`` + HTTP snapshot) で検証する。
    """
    repo = PipelineEventRepository(db_session)
    payload = AcquisitionPayload(
        fetcher_class="VentureBeatFetcher",
        http_status=403,
        final_url="https://venturebeat.com/feed/",
        response_size=1024,
        content_type="text/html",
        body_head="Forbidden",
        error_message="upstream returned 403",
        error_chain=["httpx.HTTPStatusError"],
    )

    await repo.append(
        stage=Stage.ACQUISITION,
        event_type=EventType.FAILED,
        outcome_code="permanent_fetch_error",
        payload=payload,
        source_id=source_row.id,
        duration_ms=42,
    )
    await db_session.commit()

    rows = (await db_session.execute(select(PipelineEvent))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.stage == "acquisition"
    assert row.event_type == "failed"
    assert row.outcome_code == "permanent_fetch_error"
    assert row.source_id == source_row.id
    assert row.duration_ms == 42
    assert row.payload["kind"] == "acquisition"
    assert row.payload["fetcher_class"] == "VentureBeatFetcher"
    assert row.payload["http_status"] == 403
    assert row.payload["final_url"] == "https://venturebeat.com/feed/"
    assert row.payload["error_message"] == "upstream returned 403"
    assert row.payload["error_chain"] == ["httpx.HTTPStatusError"]
    assert row.retryability is None


@pytest.mark.asyncio
async def test_source_id_auto_filled_from_article_id(
    db_session: AsyncSession, article_row: ArticleORM
) -> None:
    """article_id だけ与えると Article.source_id を逆引きして埋める。"""
    repo = PipelineEventRepository(db_session)
    payload = EmbeddingPayload(embedding_model="gemini-embedding-001")

    await repo.append(
        stage=Stage.EMBEDDING,
        event_type=EventType.SUCCEEDED,
        outcome_code="embedded",
        payload=payload,
        article_id=article_row.id,
    )
    await db_session.commit()

    row = (await db_session.execute(select(PipelineEvent))).scalars().one()
    assert row.article_id == article_row.id
    assert row.source_id == article_row.source_id


@pytest.mark.asyncio
async def test_append_persists_retryability(
    db_session: AsyncSession, article_row: ArticleORM
) -> None:
    repo = PipelineEventRepository(db_session)
    payload = EmbeddingPayload(embedding_model="gemini-embedding-001")

    await repo.append(
        stage=Stage.EMBEDDING,
        event_type=EventType.FAILED,
        outcome_code="ai_error_network",
        payload=payload,
        article_id=article_row.id,
        retryability=Retryability.RETRYABLE,
    )
    await db_session.commit()

    row = (await db_session.execute(select(PipelineEvent))).scalars().one()
    assert row.retryability == "retryable"


@pytest.mark.asyncio
async def test_append_with_no_ids_leaves_both_null(
    db_session: AsyncSession,
) -> None:
    repo = PipelineEventRepository(db_session)
    await repo.append(
        stage=Stage.DISPATCH,
        event_type=EventType.SKIPPED,
        outcome_code="no_active_sources",
        payload=AcquisitionPayload(),  # 共通基底だけ使う
    )
    await db_session.commit()

    row = (await db_session.execute(select(PipelineEvent))).scalars().one()
    assert row.source_id is None
    assert row.article_id is None


def test_stage_strenum_matches_check_constraint() -> None:
    """Stage StrEnum 値 set が ORM/migration の CHECK 制約値と一致。

    値追加時は両方の更新を要求する自然な fail-fast。10 値 (article-bound 9 stage +
    briefing)。
    """
    expected = {
        "dispatch",
        "acquisition",
        "completion",
        "curation",
        "assessment",
        "embedding",
        "backfill_curate",
        "backfill_assess",
        "backfill_embed",
        "briefing",
    }
    assert {s.value for s in Stage} == expected


def test_event_type_strenum_matches_check_constraint() -> None:
    expected = {"succeeded", "skipped", "rejected", "failed"}
    assert {e.value for e in EventType} == expected


def test_retryability_strenum_matches_check_constraint() -> None:
    expected = {"retryable", "non_retryable", "unknown"}
    assert {r.value for r in Retryability} == expected


def test_pipeline_event_top_level_columns_match_current_contract() -> None:
    """撤去済みの旧 3 列を戻さず、失敗詳細は payload に閉じることを固定する。"""
    expected = {
        "id",
        "occurred_at",
        "stage",
        "event_type",
        "outcome_code",
        "retryability",
        "source_id",
        "article_id",
        "duration_ms",
        "error_class",
        "trace_id",
        "payload",
    }
    actual = set(PipelineEvent.__table__.columns.keys())
    assert actual == expected
    assert {"category", "code", "attempt"}.isdisjoint(actual)
    assert {"failure_kind", "failure_action", "attempt_count"}.isdisjoint(actual)


@pytest.mark.asyncio
async def test_retryability_check_constraint(db_session: AsyncSession) -> None:
    """retryability 列の CHECK 制約検証: 3 値 + NULL は OK、不正値は失敗。"""
    repo = PipelineEventRepository(db_session)

    await repo.append(
        stage=Stage.DISPATCH,
        event_type=EventType.SKIPPED,
        outcome_code="test_null_retryability",
        payload=AcquisitionPayload(),
    )
    await db_session.commit()

    for retryability in Retryability:
        await repo.append(
            stage=Stage.CURATION,
            event_type=EventType.FAILED,
            outcome_code=f"test_{retryability.value}",
            payload=AcquisitionPayload(),
            retryability=retryability,
        )
    await db_session.commit()

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO pipeline_events "
                "(stage, event_type, outcome_code, retryability, payload) "
                "VALUES ('curation', 'failed', 'test', 'invalid_value', '{}')"
            )
        )
    await db_session.rollback()
