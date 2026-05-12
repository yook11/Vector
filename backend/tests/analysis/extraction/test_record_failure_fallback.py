"""``extraction/tasks.py`` の ``_record_failure`` private helper 単体テスト。

PR2 で ``failure_recording.py`` を ``tasks.py`` 内 private helper に統合した
際の挙動を Stage 4 / Stage 5 と同型に検証する:

- 正常系: 別 session で 1 行 INSERT (業務 tx と独立)
- 異常系: session_factory が常に raise → ``extraction_failure_audit_dropped``
  log fallback + business exception を再 raise しない
- 異常系 redact: business / audit exception の message に混入した
  Authorization Bearer prefix がログ field から除去される (red-team chain γ-2)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.analysis.errors import (
    AIProviderConfigurationError,
    AIProviderNetworkError,
)
from app.analysis.extraction.ai.base import BaseExtractor
from app.analysis.extraction.domain.ready import ReadyForExtraction
from app.analysis.extraction.tasks import _record_failure
from app.models.article import Article
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent


def _extractor_mock() -> MagicMock:
    mock = MagicMock(spec=BaseExtractor)
    type(mock).MODEL = "test-extract-model"
    type(mock).PROMPT_VERSION = "test-extract-prompt-v1"
    return mock


async def _make_article(db_session: AsyncSession, sample_source: NewsSource) -> Article:
    article = Article(
        source_id=sample_source.id,
        source_url="https://e.com/a",  # type: ignore[arg-type]
        original_title="t",
        original_content="body x" * 30,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    return article


def _ready(article: Article) -> ReadyForExtraction:
    return ReadyForExtraction(
        article_id=article.id,
        original_title=article.original_title,
        original_content=article.original_content,
    )


@pytest.mark.asyncio
async def test_record_failure_inserts_audit_in_separate_session(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """正常系: 別 session で audit 1 行 INSERT + commit。"""
    article = await _make_article(db_session, sample_source)
    exc = AIProviderNetworkError("conn reset")

    await _record_failure(
        session_factory,
        ready=_ready(article),
        exc=exc,
        attempt=2,
        extractor=_extractor_mock(),
    )

    rows = (
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.article_id == article.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == "failed"
    assert row.attempt == 2
    assert row.stage == "extraction"
    # PR2: 失敗 audit の ai_model / prompt_version は extractor 経由
    assert row.payload["ai_model"] == "test-extract-model"
    assert row.payload["prompt_version"] == "test-extract-prompt-v1"


@pytest.mark.asyncio
async def test_audit_insert_failure_logs_and_swallows() -> None:
    """``session_factory`` が常に raise する場合、log fallback で観測可能。

    business exception を再 raise しないことも同時に検証する
    (業務 task を audit 失敗で殺さない、best-effort 運用シグナル)。
    """

    class _BoomFactory:
        def __call__(self) -> Any:
            raise RuntimeError("db down")

    ready = ReadyForExtraction(
        article_id=42, original_title="t", original_content="c" * 60
    )
    business_exc = AIProviderConfigurationError("api key missing")

    with capture_logs() as cap:
        await _record_failure(
            _BoomFactory(),  # type: ignore[arg-type]
            ready=ready,
            exc=business_exc,
            attempt=3,
            extractor=_extractor_mock(),
        )

    drops = [e for e in cap if e.get("event") == "extraction_failure_audit_dropped"]
    assert drops, "fallback ログが emit されていない"
    drop = drops[-1]
    assert drop["article_id"] == 42
    assert drop["attempt"] == 3
    assert drop["business_error_class"].endswith(".AIProviderConfigurationError")
    assert drop["business_error_message"] == "api key missing"
    assert drop["audit_error_class"].endswith(".RuntimeError")


@pytest.mark.asyncio
async def test_audit_insert_failure_log_redacts_secrets() -> None:
    """log fallback の error_message field に secret prefix を漏らさない。

    red-team chain γ-2 対称化: DB payload (``audit_repository.py``) と同様に
    log 経路にも ``redact_secrets`` を通して Authorization Bearer / API key
    prefix がログから消えていることを確認する。
    """

    class _BoomFactory:
        def __call__(self) -> Any:
            # audit_exc 側にも secret を混ぜて両方の redact を検証する
            raise RuntimeError("boom Authorization: Bearer sk-live-AUDITSECRETxyz")

    ready = ReadyForExtraction(
        article_id=99, original_title="t", original_content="c" * 60
    )
    business_exc = AIProviderNetworkError(
        "upstream failed Authorization: Bearer sk-live-BUSINESSSECRETabc"
    )

    with capture_logs() as cap:
        await _record_failure(
            _BoomFactory(),  # type: ignore[arg-type]
            ready=ready,
            exc=business_exc,
            attempt=1,
            extractor=_extractor_mock(),
        )

    drops = [e for e in cap if e.get("event") == "extraction_failure_audit_dropped"]
    assert drops, "fallback ログが emit されていない"
    drop = drops[-1]
    assert "sk-live-BUSINESSSECRETabc" not in drop["business_error_message"]
    assert "sk-live-AUDITSECRETxyz" not in drop["audit_error_message"]
