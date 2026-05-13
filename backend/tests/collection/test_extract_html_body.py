"""``extract_html_body`` task の振る舞い不変条件テスト (PR2.5-B 仕様 + PR3 案 3)。

task は ``ContentFetchService`` への薄ラッパー。本テストの責務は **戻り値
dispatch のみ** で、以下は対象外 (それぞれ別ファイル):

- Service 内部 (HTTP 取得 / DB 永続化 / pipeline_events / 各失敗 reason_code):
  ``tests/collection/extraction/test_content_fetch_service.py``
- ReadyForExtraction gatekeeper (extraction/noise 既存判定 / 本文長 cap):
  下流 Stage 3 task と ``ExtractionRepository.try_load_for_extraction`` の
  責務 (PR3 案 3 化)。本 task は ID-only ``ExtractionTrigger`` を kiq に渡すのみ

検証する task 不変条件:

- ``int`` (article_id) → ``extract_content.kiq`` を
  ``ExtractionTrigger(article_id)`` で発火 + success dict 返却
- ``None`` (重複配送 / lease 衝突 / 永続失敗 / 一時失敗 / race-loss) → ``None``
  返却、chain 発火せず
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.extraction.domain.ready import ExtractionTrigger
from app.collection.tasks import extract_html_body

_SERVICE_EXECUTE = (
    "app.collection.extraction.content_fetch_service.ContentFetchService.execute"
)
_EXTRACT_CONTENT_KIQ = "app.analysis.extraction.tasks.extract_content.kiq"


def _ctx(session_factory: async_sessionmaker[AsyncSession]) -> MagicMock:
    """taskiq Context の最小 mock。task は session_factory のみ参照する。"""
    ctx = MagicMock()
    ctx.state.session_factory = session_factory
    return ctx


@pytest.mark.asyncio
async def test_chains_extract_content_with_trigger_when_article_id_returned(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``int`` (article_id) → ``extract_content.kiq`` を Trigger で発火 + success dict."""  # noqa: E501
    monkeypatch.setattr(_SERVICE_EXECUTE, AsyncMock(return_value=123))
    extract_content_kiq = AsyncMock()
    monkeypatch.setattr(_EXTRACT_CONTENT_KIQ, extract_content_kiq)

    result = await extract_html_body(pending_id=42, ctx=_ctx(session_factory))

    assert result == {
        "pending_id": 42,
        "article_id": 123,
        "status": "success",
    }
    extract_content_kiq.assert_awaited_once_with(ExtractionTrigger(article_id=123))


@pytest.mark.asyncio
async def test_returns_none_when_service_returns_none(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service が ``None`` を返したら task も ``None`` 返却、chain は発火しない。"""
    monkeypatch.setattr(_SERVICE_EXECUTE, AsyncMock(return_value=None))
    extract_content_kiq = AsyncMock()
    monkeypatch.setattr(_EXTRACT_CONTENT_KIQ, extract_content_kiq)

    result = await extract_html_body(pending_id=123, ctx=_ctx(session_factory))

    assert result is None
    extract_content_kiq.assert_not_awaited()
