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

from app.models.article import Article as ArticleORM
from app.models.news_source import NewsSource, SourceType
from app.models.pipeline_event import PipelineEvent
from app.observability.categories import Layer1Category
from app.observability.domain.event import EventType, Stage
from app.observability.domain.payloads import (
    EmbeddingPayload,
    SourceFetchPayload,
)
from app.observability.repository import PipelineEventRepository


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
    repo = PipelineEventRepository(db_session)
    payload = SourceFetchPayload(
        fetcher_class="VentureBeatFetcher",
        entry_count=6,
        article_created_count=3,
        completion_queued_count=2,
        skipped_count=0,
        failed_count=1,
        failed_codes={"http_403": 1},
    )

    await repo.append(
        stage=Stage.SOURCE_FETCH,
        event_type=EventType.SUCCEEDED,
        outcome_code="fetched",
        payload=payload,
        source_id=source_row.id,
        attempt=1,
        duration_ms=42,
    )
    await db_session.commit()

    rows = (await db_session.execute(select(PipelineEvent))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.stage == "source_fetch"
    assert row.event_type == "succeeded"
    assert row.outcome_code == "fetched"
    assert row.source_id == source_row.id
    assert row.duration_ms == 42
    assert row.payload["kind"] == "source_fetch"
    assert row.payload["fetcher_class"] == "VentureBeatFetcher"
    assert row.payload["entry_count"] == 6
    assert row.payload["article_created_count"] == 3
    assert row.payload["completion_queued_count"] == 2
    assert row.payload["failed_codes"] == {"http_403": 1}


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
async def test_append_with_no_ids_leaves_both_null(
    db_session: AsyncSession,
) -> None:
    repo = PipelineEventRepository(db_session)
    await repo.append(
        stage=Stage.DISPATCH,
        event_type=EventType.SKIPPED,
        outcome_code="no_active_sources",
        payload=SourceFetchPayload(),  # 共通基底だけ使う
    )
    await db_session.commit()

    row = (await db_session.execute(select(PipelineEvent))).scalars().one()
    assert row.source_id is None
    assert row.article_id is None


def test_stage_strenum_matches_check_constraint() -> None:
    """Stage StrEnum 値 set が ORM/migration の CHECK 制約値と一致。

    値追加時は両方の更新を要求する自然な fail-fast。
    """
    expected = {
        "dispatch",
        "source_fetch",
        "content_fetch",
        "extraction",
        "classification",
        "embedding",
        "backfill_extract",
        "backfill_classify",
        "backfill_embed",
    }
    assert {s.value for s in Stage} == expected


def test_event_type_strenum_matches_check_constraint() -> None:
    expected = {"succeeded", "skipped", "rejected", "failed"}
    assert {e.value for e in EventType} == expected


@pytest.mark.asyncio
async def test_category_check_constraint(db_session: AsyncSession) -> None:
    """category 列の CHECK 制約検証: 6 値 + NULL は OK、不正値で IntegrityError。

    Layer1Category は article-bound analysis stages 専用の語彙のため、collection 系
    stage (dispatch / source_fetch / content_fetch) では NULL のまま記録される。
    DB CHECK は ``category IS NULL OR category IN (6 values)`` の形で NULL を許容。
    """
    repo = PipelineEventRepository(db_session)

    # NULL (category 引数省略) は OK — collection 系 stage の通常パス
    await repo.append(
        stage=Stage.DISPATCH,
        event_type=EventType.SKIPPED,
        outcome_code="test_null_category",
        payload=SourceFetchPayload(),
    )
    await db_session.commit()

    # 6 値はすべて OK — article-bound analysis stages の正規パス
    for cat in Layer1Category:
        await repo.append(
            stage=Stage.EXTRACTION,
            event_type=EventType.FAILED,
            outcome_code=f"test_{cat.value}",
            payload=SourceFetchPayload(),
            category=cat,
        )
    await db_session.commit()

    # 不正値は IntegrityError (raw SQL で挿入を試みる)
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO pipeline_events "
                "(stage, event_type, outcome_code, category, attempt, payload) "
                "VALUES ('extraction', 'failed', 'test', 'invalid_value', 1, '{}')"
            )
        )
    await db_session.rollback()
