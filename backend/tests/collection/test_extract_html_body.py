"""``extract_html_body`` task の振る舞い不変条件テスト (案 3: 厚い Ready 自構築)。

task は処理開始時に ``ReadyForArticleCompletion.try_advance_from`` で厚い Ready を
自構築し、Ready を ``ArticleCompletionService.execute(ready)`` に渡す薄ラッパー。
本テストの責務は **Ready 構築の短絡 + 戻り値 dispatch** で、以下は対象外
(それぞれ別ファイル):

- Service 内部 (HTTP 取得 / DB 永続化 / 各失敗 reason_code):
  ``tests/collection/article_completion/test_service.py``
- precondition gateway (``status='running'`` 判定 / Ready 物体化):
  ``ArticleCompletionRepository.try_load_for_completion`` の責務 →
  ``tests/collection/article_completion/test_repository.py``
- 下流 Stage 3 は ID-only ``ExtractionTrigger`` を kiq に渡すのみ

検証する task 不変条件:

- precondition 未充足 (``try_advance_from`` が ``None``) → skip log + ``None``
  返却、Service 不構築 / chain 発火せず
- ``int`` (article_id) → ``extract_content.kiq`` を
  ``ExtractionTrigger(article_id)`` で発火 + success dict 返却
- ``None`` (lease 衝突 / 永続失敗 / 一時失敗 / race-loss) → ``None``
  返却、chain 発火せず
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.extraction.domain.ready import ExtractionTrigger
from app.collection.article_completion.ready import ReadyForArticleCompletion
from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.source_completion_profile import DEFAULT_PROFILE
from app.collection.domain.value_objects import PublishedAt
from app.collection.tasks import extract_html_body
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl
from app.shared.value_objects.source_name import SourceName

_SERVICE_EXECUTE = (
    "app.collection.article_completion.service.ArticleCompletionService.execute"
)
_SERVICE_CLS = "app.collection.article_completion.service.ArticleCompletionService"
_EXTRACT_CONTENT_KIQ = "app.analysis.extraction.tasks.extract_content.kiq"


def _ctx(session_factory: async_sessionmaker[AsyncSession]) -> MagicMock:
    """taskiq Context の最小 mock。task は session_factory のみ参照する。"""
    ctx = MagicMock()
    ctx.state.session_factory = session_factory
    return ctx


def _fixed_ready(pending_id: int = 42) -> ReadyForArticleCompletion:
    """task 冒頭の Ready 自構築が返す固定 Ready。"""
    url = CanonicalArticleUrl("https://example.com/a")
    return ReadyForArticleCompletion(
        pending_id=pending_id,
        source_id=1,
        attempt_count=1,
        observed=ObservedArticle(
            source_name=SourceName("Example"),
            source_url=url,
            title=ObservedField(value="Title", origin=ObservedOrigin.feed),
            published_at=ObservedField(
                value=PublishedAt(datetime(2026, 5, 1, tzinfo=UTC)),
                origin=ObservedOrigin.feed,
            ),
        ),
        profile=DEFAULT_PROFILE,
        source_url=url,
    )


def _patch_try_advance_from(ready: ReadyForArticleCompletion | None) -> object:
    """``ReadyForArticleCompletion.try_advance_from`` を固定値返却に patch する。"""
    return patch.object(
        ReadyForArticleCompletion,
        "try_advance_from",
        new=AsyncMock(return_value=ready),
    )


@pytest.mark.asyncio
async def test_precondition_not_met_skips_and_does_not_call_service(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``try_advance_from`` が ``None`` → skip + ``None``、Service / chain 不発火。

    案 3: precondition (未 claim / sweep 済 / close 済 / delete 済) の判定は
    task 冒頭の Ready 自構築時に行われ、未充足なら Service を消費せず短絡する。
    """
    with (
        _patch_try_advance_from(None),
        patch(_SERVICE_CLS) as mock_svc_cls,
        patch(_EXTRACT_CONTENT_KIQ) as mock_kiq,
    ):
        result = await extract_html_body(pending_id=999, ctx=_ctx(session_factory))

    assert result is None
    mock_svc_cls.assert_not_called()
    mock_kiq.assert_not_awaited()


@pytest.mark.asyncio
async def test_chains_extract_content_with_trigger_when_article_id_returned(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``int`` (article_id) → ``extract_content.kiq`` を Trigger で発火 + success dict."""  # noqa: E501
    extract_content_kiq = AsyncMock()
    monkeypatch.setattr(_SERVICE_EXECUTE, AsyncMock(return_value=123))
    monkeypatch.setattr(_EXTRACT_CONTENT_KIQ, extract_content_kiq)

    with _patch_try_advance_from(_fixed_ready(pending_id=42)):
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
    extract_content_kiq = AsyncMock()
    monkeypatch.setattr(_SERVICE_EXECUTE, AsyncMock(return_value=None))
    monkeypatch.setattr(_EXTRACT_CONTENT_KIQ, extract_content_kiq)

    with _patch_try_advance_from(_fixed_ready(pending_id=123)):
        result = await extract_html_body(pending_id=123, ctx=_ctx(session_factory))

    assert result is None
    extract_content_kiq.assert_not_awaited()
