"""``extract_html_body`` task の振る舞い不変条件テスト (PR2.5-B 仕様)。

task は ``ContentFetchService`` への薄ラッパー。本テストの責務は **Outcome
dispatch のみ** で、以下は対象外 (それぞれ別ファイル):

- Service 内部 (HTTP 取得 / DB 永続化 / pipeline_events / Outcome 構築):
  ``tests/collection/extraction/test_content_fetch_service.py``
- ReadyForExtraction gatekeeper (extraction/noise 既存判定 / 本文長 cap):
  ``tests/analysis/extraction/domain/test_ready.py``

検証する task 不変条件:

- ``ContentFetched(article)`` + Ready 構築成功 → ``extract_content.kiq`` 発火
  + success dict 返却
- ``ContentFetched(article)`` + Ready=None (gatekeeper 拒否) → kiq 不発、
  だが Article 永続化は完了しているので success dict は返す
- ``ConflictLost`` / ``TerminallyDropped`` / ``TransientlyDropped`` /
  ``None`` (重複配送) → ``None`` 返却、chain 発火せず

Service mock により pending_html_articles の DB 仕込みは不要。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.extraction.content_fetch_service import (
    ConflictLost,
    ContentFetched,
    TerminallyDropped,
    TransientlyDropped,
)
from app.collection.extraction.domain import Article
from app.collection.extraction.domain.value_objects import PublishedAt
from app.collection.tasks import extract_html_body

_SERVICE_EXECUTE = (
    "app.collection.extraction.content_fetch_service.ContentFetchService.execute"
)
_READY_TRY_ADVANCE = (
    "app.analysis.extraction.domain.ready.ReadyForExtraction.try_advance_from"
)
_EXTRACT_CONTENT_KIQ = "app.analysis.tasks.extract_content.kiq"


def _ctx(session_factory: async_sessionmaker[AsyncSession]) -> MagicMock:
    """taskiq Context の最小 mock。task は session_factory のみ参照する。"""
    ctx = MagicMock()
    ctx.state.session_factory = session_factory
    return ctx


def _make_article(article_id: int = 1) -> Article:
    """test 入力用の Article Entity (DB 永続化はしない)。

    ``ReadyForExtraction.try_advance_from`` は article_extractions /
    extraction_noises を SELECT するだけで articles 自体の存在は問わないため、
    domain Entity を直接組み立てれば十分 (db_session は使わない)。
    """
    return Article(
        id=article_id,
        discovered_article_id=None,
        article_url_id=10,
        title="Test Title",
        body="x" * 100,
        published_at=PublishedAt(datetime(2026, 5, 1, tzinfo=UTC)),
        created_at=datetime(2026, 5, 6, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_chains_extract_content_when_ready_is_built(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ContentFetched + Ready 構築成功 → ``extract_content.kiq`` 発火 + success dict."""
    article = _make_article()
    monkeypatch.setattr(
        _SERVICE_EXECUTE,
        AsyncMock(return_value=ContentFetched(article=article)),
    )
    extract_content_kiq = AsyncMock()
    monkeypatch.setattr(_EXTRACT_CONTENT_KIQ, extract_content_kiq)

    result = await extract_html_body(pending_id=123, ctx=_ctx(session_factory))

    assert result == {
        "pending_id": 123,
        "article_id": article.id,
        "status": "success",
    }
    extract_content_kiq.assert_awaited_once()
    (ready_arg,) = extract_content_kiq.await_args.args
    assert ready_arg.article_id == article.id
    assert ready_arg.original_title == article.title
    assert ready_arg.original_content == article.body


@pytest.mark.asyncio
async def test_skips_chain_when_ready_returns_none(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ContentFetched + Ready=None (gatekeeper 拒否) → kiq 不発、success dict 返却."""
    article = _make_article()
    monkeypatch.setattr(
        _SERVICE_EXECUTE,
        AsyncMock(return_value=ContentFetched(article=article)),
    )
    monkeypatch.setattr(_READY_TRY_ADVANCE, AsyncMock(return_value=None))
    extract_content_kiq = AsyncMock()
    monkeypatch.setattr(_EXTRACT_CONTENT_KIQ, extract_content_kiq)

    result = await extract_html_body(pending_id=123, ctx=_ctx(session_factory))

    assert result == {
        "pending_id": 123,
        "article_id": article.id,
        "status": "success",
    }
    extract_content_kiq.assert_not_awaited()


@pytest.mark.parametrize(
    "outcome",
    [
        ConflictLost(),
        TerminallyDropped(reason_code="permanent_fetch_error"),
        TransientlyDropped(reason_code="temporary_will_retry_server_error"),
        None,
    ],
    ids=["conflict_lost", "terminally_dropped", "transiently_dropped", "service_none"],
)
@pytest.mark.asyncio
async def test_returns_none_for_non_success_outcomes(
    outcome: ConflictLost | TerminallyDropped | TransientlyDropped | None,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功以外の Outcome (4 variant) → None 返却、chain は発火しない."""
    monkeypatch.setattr(_SERVICE_EXECUTE, AsyncMock(return_value=outcome))
    extract_content_kiq = AsyncMock()
    monkeypatch.setattr(_EXTRACT_CONTENT_KIQ, extract_content_kiq)

    result = await extract_html_body(pending_id=123, ctx=_ctx(session_factory))

    assert result is None
    extract_content_kiq.assert_not_awaited()
