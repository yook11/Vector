"""``ExtractionService.mark_article_unprocessable`` の integration test (PR3-a-1)。

検証する性質:
- 1 tx 内で audit INSERT → article DELETE が両方完了する
- 順序: audit が先 (source_id 自動補完が article 健在時に確定)
- DELETE 後、``articles`` から該当 row が消える
- ``pipeline_events.article_id`` は ``ondelete=SET NULL`` のため audit 行は残る
  ただし新規 INSERT 時点では ``article_id`` が埋まっている (DELETE 前)
- ``source_id`` が auto-resolve される (article DELETE 後でも source 追跡可能)
- ``source_name`` が payload に保存される (FK 切断耐性)
- ``ExtractionPolicyBlockedError`` の ``raw_response`` が
  ``ai_raw_response`` に焼かれる (best-effort)
- ``ExtractionInputTooLargeError`` では ``ai_raw_response`` なし
- 失敗 outcome_code が CHECK 制約値内 (failed)
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.extraction.extractor.errors import (
    ExtractionInputTooLargeError,
    ExtractionPolicyBlockedError,
)
from app.analysis.extraction.extractor.gemini_prompt import GeminiExtractionPrompt
from app.analysis.extraction.service import ExtractionService
from app.models.article import Article
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent


async def _make_article(
    db_session: AsyncSession,
    sample_source: NewsSource,
    *,
    url: str = "https://e.com/a",
    content: str = "body content " * 30,
) -> Article:
    article = Article(
        source_id=sample_source.id,
        source_url=url,  # type: ignore[arg-type]
        original_title="t",
        original_content=content,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    return article


@pytest.mark.asyncio
async def test_blocked_by_policy_writes_audit_then_deletes_article(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    article = await _make_article(db_session, sample_source)
    article_id = article.id
    # rollback 後の expired-attr lazy reload を避けるため事前に値を取り出す
    expected_source_id = sample_source.id
    expected_source_name = str(sample_source.name)
    svc = ExtractionService(session_factory)

    exc = ExtractionPolicyBlockedError(
        finish_reason="SAFETY",
        raw_response='{"draft":"sensitive"}',
        prompt_version=GeminiExtractionPrompt.VERSION,
    )
    await svc.mark_article_unprocessable(
        article_id,
        article.original_content,
        outcome_code="ai_error_blocked_by_policy",
        exc=exc,
    )

    # commit が走った別 tx の DB 状態を確認するため fresh session で検証
    await db_session.rollback()
    article_row = await db_session.get(Article, article_id)
    assert article_row is None  # DELETE 済

    events = list(
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.stage == "extraction")
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "failed"
    assert ev.outcome_code == "ai_error_blocked_by_policy"
    # SET NULL: article_id は NULL に
    assert ev.article_id is None
    # source_id は auto-resolve で埋まっている (DELETE 前に INSERT したため)
    assert ev.source_id == expected_source_id
    payload = ev.payload
    assert payload["source_name"] == expected_source_name
    assert payload["ai_model"] == GeminiExtractionPrompt.MODEL
    assert payload["error_message"] is not None
    assert payload["error_chain"] is not None
    assert payload["ai_raw_response"] == '{"draft":"sensitive"}'


@pytest.mark.asyncio
async def test_input_too_large_writes_audit_then_deletes_article(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    article = await _make_article(db_session, sample_source)
    article_id = article.id
    svc = ExtractionService(session_factory)

    exc = ExtractionInputTooLargeError(prompt_version=GeminiExtractionPrompt.VERSION)
    await svc.mark_article_unprocessable(
        article_id,
        article.original_content,
        outcome_code="ai_error_input_too_large",
        exc=exc,
    )

    await db_session.rollback()
    assert (await db_session.get(Article, article_id)) is None
    events = list(
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.stage == "extraction")
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.outcome_code == "ai_error_input_too_large"
    assert ev.event_type == "failed"
    # context length 例外は raw_response 持たないので ai_raw_response key なし or None
    assert ev.payload.get("ai_raw_response") is None
