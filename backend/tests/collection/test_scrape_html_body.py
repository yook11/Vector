"""``scrape_html_body`` task の振る舞い不変条件テスト (案 3: 厚い Ready 自構築)。

task は処理開始時に ``ReadyForArticleCompletion.try_advance_from`` で厚い Ready を
自構築し、Ready を ``ArticleCompletionService.execute(ready)`` に渡す薄ラッパー。
本テストの責務は **Ready 構築の短絡 + 戻り値 dispatch** で、以下は対象外
(それぞれ別ファイル):

- Service 内部 (HTTP 取得 / DB 永続化 / 各失敗 reason_code):
  ``tests/collection/article_completion/test_service.py``
- precondition gateway (pending lifecycle 判定 / Ready 構築):
  ``tests/collection/article_completion/test_repository.py``
- 下流 Stage 3 は ID-only ``CurationTrigger`` を kiq に渡すのみ

検証する task 不変条件:

- Ready build blocked → skipped audit + ``None`` 返却、Service 不構築 / chain 不発火
- Ready build failed → failed audit 後に re-raise、Service 不構築 / chain 不発火
- ``int`` (analyzable_article_id) → ``curate_content.kiq`` を
  ``CurationTrigger(analyzable_article_id)`` で発火 + success dict 返却
- ``None`` (lease 衝突 / 永続失敗 / 一時失敗 / race-loss) → ``None``
  返却、chain 発火せず
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.article_completion.ready import (
    ArticleCompletionReadyBuildIncompleteArticleMissingError,
    ReadyForArticleCompletion,
)
from app.collection.domain.canonical_article_url import CanonicalArticleUrl
from app.collection.domain.observed_article import (
    ObservedArticle,
    ObservedField,
    ObservedOrigin,
)
from app.collection.domain.value_objects import PublishedAt
from app.collection.sources.article_completion_policy import DEFAULT_POLICY
from app.collection.sources.errors import SourceNotRegisteredError
from app.collection.sources.source_name import SourceName
from app.queue.messages.curation import CurationTrigger
from app.queue.tasks.completion import scrape_html_body

_SERVICE_EXECUTE = (
    "app.collection.article_completion.service.ArticleCompletionService.execute"
)
_SERVICE_CLS = "app.queue.tasks.completion.ArticleCompletionService"
_CURATE_CONTENT_KIQ = "app.queue.tasks.completion.curate_content.kiq"


def _ctx(session_factory: async_sessionmaker[AsyncSession]) -> MagicMock:
    """taskiq Context の最小 mock。task は session_factory のみ参照する。"""
    ctx = MagicMock()
    ctx.state.session_factory = session_factory
    return ctx


def _fixed_ready(incomplete_article_id: int = 42) -> ReadyForArticleCompletion:
    """task 冒頭の Ready 自構築が返す固定 Ready。"""
    url = CanonicalArticleUrl("https://example.com/a")
    return ReadyForArticleCompletion(
        incomplete_article_id=incomplete_article_id,
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
        completion_policy=DEFAULT_POLICY,
        source_url=url,
    )


def _patch_try_advance_from(
    result: ReadyForArticleCompletion | Exception,
) -> object:
    """``ReadyForArticleCompletion.try_advance_from`` を固定値返却に patch する。"""
    if isinstance(result, Exception):
        mock = AsyncMock(side_effect=result)
    else:
        mock = AsyncMock(return_value=result)
    return patch.object(
        ReadyForArticleCompletion,
        "try_advance_from",
        new=mock,
    )


@pytest.mark.asyncio
async def test_ready_build_skipped_error_audits_and_does_not_call_service(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ready build skipped error → audit + return、Service / chain 不発火。"""
    exc = ArticleCompletionReadyBuildIncompleteArticleMissingError()
    with (
        _patch_try_advance_from(exc),
        patch("app.queue.tasks.completion.ArticleCompletionAuditRepository") as audit,
        patch(_SERVICE_CLS) as mock_svc_cls,
        patch(_CURATE_CONTENT_KIQ) as mock_kiq,
    ):
        audit.return_value.append_ready_build_error = AsyncMock()
        result = await scrape_html_body(
            incomplete_article_id=999, ctx=_ctx(session_factory)
        )

    assert result is None
    audit.return_value.append_ready_build_error.assert_awaited_once_with(
        incomplete_article_id=999,
        exc=exc,
        facts=None,
    )
    mock_svc_cls.assert_not_called()
    mock_kiq.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_build_failed_error_audits_and_reraises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ready build failed error は audit 後に元例外を raise する。"""
    exc = SourceNotRegisteredError()

    with (
        _patch_try_advance_from(exc),
        patch(
            "app.queue.tasks.completion._append_ready_build_error_audit",
            new=AsyncMock(),
        ) as audit_error,
        patch(_SERVICE_CLS) as mock_svc_cls,
        patch(_CURATE_CONTENT_KIQ) as mock_kiq,
    ):
        with pytest.raises(SourceNotRegisteredError):
            await scrape_html_body(incomplete_article_id=999, ctx=_ctx(session_factory))

    audit_error.assert_awaited_once_with(
        session_factory,
        incomplete_article_id=999,
        exc=exc,
    )
    mock_svc_cls.assert_not_called()
    mock_kiq.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_build_unexpected_exception_audits_and_reraises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ready 判定中の想定外例外も fallback audit 後に元例外を raise する。"""
    exc = RuntimeError("ready build exploded")

    with (
        _patch_try_advance_from(exc),
        patch(
            "app.queue.tasks.completion._append_ready_build_error_audit",
            new=AsyncMock(),
        ) as audit_error,
        patch(_SERVICE_CLS) as mock_svc_cls,
        patch(_CURATE_CONTENT_KIQ) as mock_kiq,
    ):
        with pytest.raises(RuntimeError):
            await scrape_html_body(incomplete_article_id=999, ctx=_ctx(session_factory))

    audit_error.assert_awaited_once_with(
        session_factory,
        incomplete_article_id=999,
        exc=exc,
    )
    mock_svc_cls.assert_not_called()
    mock_kiq.assert_not_awaited()


@pytest.mark.asyncio
async def test_chains_curate_content_with_trigger_when_article_id_returned(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``int`` (analyzable_article_id) → ``curate_content.kiq`` を Trigger で発火 + success dict."""  # noqa: E501
    curate_content_kiq = AsyncMock()
    monkeypatch.setattr(_SERVICE_EXECUTE, AsyncMock(return_value=123))
    monkeypatch.setattr(_CURATE_CONTENT_KIQ, curate_content_kiq)

    with _patch_try_advance_from(_fixed_ready(incomplete_article_id=42)):
        result = await scrape_html_body(
            incomplete_article_id=42, ctx=_ctx(session_factory)
        )

    assert result == {
        "incomplete_article_id": 42,
        "analyzable_article_id": 123,
        "status": "success",
    }
    curate_content_kiq.assert_awaited_once_with(
        CurationTrigger(analyzable_article_id=123)
    )


@pytest.mark.asyncio
async def test_returns_none_when_service_returns_none(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Service が ``None`` を返したら task も ``None`` 返却、chain は発火しない。"""
    curate_content_kiq = AsyncMock()
    monkeypatch.setattr(_SERVICE_EXECUTE, AsyncMock(return_value=None))
    monkeypatch.setattr(_CURATE_CONTENT_KIQ, curate_content_kiq)

    with _patch_try_advance_from(_fixed_ready(incomplete_article_id=123)):
        result = await scrape_html_body(
            incomplete_article_id=123, ctx=_ctx(session_factory)
        )

    assert result is None
    curate_content_kiq.assert_not_awaited()
