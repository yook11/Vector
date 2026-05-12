"""``extract_content`` task の Layer 1 marker dispatch routing テスト (PR3.5-c)。

Service の execute を mock して、tasks.py が **どの Layer 1 marker** を受けて
**どこ** (mark_article_unprocessable / _record_failure / inline retry /
catch-all) に振り分けるかを検証する。

実 DB / 実 Service / 実 audit_repository は呼ばない:
- ``ExtractionService`` を patch
- ``_record_failure`` を patch (Stage 3 failure 経路の Task 層 private helper、
  PR2 で ``failure_recording.py`` から移管)
- ``mark_article_unprocessable`` を patch

PR3 案 3 化: task signature は ``trigger: ExtractionTrigger``。冒頭の Ready 自構築
は ``ReadyForExtraction.try_advance_from`` を patch して固定 Ready を返させる。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis.errors import (
    AIProviderConfigurationError,
    AIProviderInputRejectedError,
    AIProviderInsufficientBalanceError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
    AIProviderQuotaExhaustedError,
    AIProviderRateLimitedError,
    AIProviderRequestInvalidError,
    AIProviderServiceUnavailableError,
    ExtractionResponseInvalidError,
)
from app.analysis.extraction.domain.ready import ExtractionTrigger, ReadyForExtraction


def _make_provider_fake() -> MagicMock:
    fake = MagicMock()
    fake.MODEL = "test-model"
    fake.PROMPT_VERSION = "test-prompt-v1"
    fake.RPM = 50
    fake.RPD = 1500
    return fake


def _make_ctx(retry_count: int = 0, max_retries: int = 1) -> MagicMock:
    ctx = MagicMock()
    ctx.state = SimpleNamespace(session_factory=MagicMock())
    ctx.state.extractor = _make_provider_fake()
    ctx.message.labels = {
        "retry_count": retry_count,
        "max_retries": max_retries,
    }
    return ctx


def _trigger() -> ExtractionTrigger:
    return ExtractionTrigger(article_id=42)


def _fixed_ready() -> ReadyForExtraction:
    """task 冒頭の Ready 自構築が返す固定 Ready (Repository を mock するため)。"""
    return ReadyForExtraction(
        article_id=42, original_title="t", original_content="content body"
    )


def _patch_try_advance_from(ready: ReadyForExtraction | None = None) -> object:
    """``ReadyForExtraction.try_advance_from`` を固定値返却に patch するヘルパ。"""
    return patch.object(
        ReadyForExtraction,
        "try_advance_from",
        new=AsyncMock(return_value=ready if ready is not None else _fixed_ready()),
    )


# ---------------------------------------------------------------------------
# NonRetryableDropArticle 経路 — mark_article_unprocessable で audit + DELETE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc_cls", "expected_code"),
    [
        (AIProviderInputRejectedError, "ai_error_input_rejected"),
        (AIProviderOutputBlockedError, "ai_error_output_blocked"),
    ],
)
@pytest.mark.asyncio
async def test_drop_article_calls_mark_unprocessable_with_correct_code(
    exc_cls: type[Exception], expected_code: str
) -> None:
    """Drop 系 2 種は mark_article_unprocessable に dispatch される。"""
    from app.analysis.extraction.tasks import extract_content

    ctx = _make_ctx()
    exc = exc_cls("boom")

    with (
        _patch_try_advance_from(),
        patch(
            "app.analysis.extraction.tasks._build_limiters", return_value=(None, None)
        ),
        patch("app.analysis.extraction.tasks.ExtractionService") as mock_svc_cls,
    ):
        svc_instance = mock_svc_cls.return_value
        svc_instance.execute = AsyncMock(side_effect=exc)
        svc_instance.mark_article_unprocessable = AsyncMock()
        await extract_content(trigger=_trigger(), ctx=ctx)
        svc_instance.mark_article_unprocessable.assert_awaited_once()
        kwargs = svc_instance.mark_article_unprocessable.await_args.kwargs
        args = svc_instance.mark_article_unprocessable.await_args.args
        assert args[0] == 42  # article_id
        assert kwargs["code"] == expected_code
        assert kwargs["exc"] is exc
        # PR2: extractor を Task 層から渡している (Service には extractor を保持しない)
        assert kwargs["extractor"] is ctx.state.extractor


# ---------------------------------------------------------------------------
# NonRetryableKeepArticle 経路 — _audit_extraction_failure (記事保持)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_cls",
    [
        AIProviderConfigurationError,
        AIProviderRequestInvalidError,
        AIProviderInsufficientBalanceError,
    ],
)
@pytest.mark.asyncio
async def test_keep_article_calls_audit_extraction_failure(
    exc_cls: type[Exception],
) -> None:
    """NonRetryableKeepArticle 系 (Layer 2-A の 3 種) は audit のみで記事保持。"""
    from app.analysis.extraction.tasks import extract_content

    ctx = _make_ctx()
    with (
        _patch_try_advance_from(),
        patch(
            "app.analysis.extraction.tasks._build_limiters", return_value=(None, None)
        ),
        patch("app.analysis.extraction.tasks.ExtractionService") as mock_svc_cls,
        patch(
            "app.analysis.extraction.tasks._record_failure",
            new=AsyncMock(),
        ) as mock_audit,
    ):
        svc_instance = mock_svc_cls.return_value
        svc_instance.execute = AsyncMock(side_effect=exc_cls("boom"))
        svc_instance.mark_article_unprocessable = AsyncMock()
        await extract_content(trigger=_trigger(), ctx=ctx)
    mock_audit.assert_awaited_once()
    assert mock_audit.await_args.kwargs["exc"].__class__ is exc_cls
    # PR2: extractor を Task 層から渡している (failure_recording.py 統合)
    assert mock_audit.await_args.kwargs["extractor"] is ctx.state.extractor
    # mark_article_unprocessable は呼ばれない
    svc_instance.mark_article_unprocessable.assert_not_awaited()


# ---------------------------------------------------------------------------
# RetryableError + INLINE_RETRY=True — taskiq retry が走る
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_cls",
    [
        AIProviderNetworkError,
        AIProviderServiceUnavailableError,
        ExtractionResponseInvalidError,
    ],
)
@pytest.mark.asyncio
async def test_retryable_inline_true_raises_when_not_last_attempt(
    exc_cls: type[Exception],
) -> None:
    """INLINE_RETRY=True の RetryableError は not is_last_attempt なら raise する。"""
    from app.analysis.extraction.tasks import extract_content

    ctx = _make_ctx(retry_count=0, max_retries=1)  # retry 余地あり

    with (
        _patch_try_advance_from(),
        patch(
            "app.analysis.extraction.tasks._build_limiters", return_value=(None, None)
        ),
        patch("app.analysis.extraction.tasks.ExtractionService") as mock_svc_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc_cls("boom"))
        with pytest.raises(exc_cls):
            await extract_content(trigger=_trigger(), ctx=ctx)


@pytest.mark.asyncio
async def test_retryable_inline_true_audits_on_last_attempt() -> None:
    """INLINE_RETRY=True でも is_last_attempt なら audit + return (cron 救済委譲)。"""
    from app.analysis.extraction.tasks import extract_content

    ctx = _make_ctx(retry_count=1, max_retries=1)  # 最終試行

    with (
        _patch_try_advance_from(),
        patch(
            "app.analysis.extraction.tasks._build_limiters", return_value=(None, None)
        ),
        patch("app.analysis.extraction.tasks.ExtractionService") as mock_svc_cls,
        patch(
            "app.analysis.extraction.tasks._record_failure",
            new=AsyncMock(),
        ) as mock_audit,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(
            side_effect=AIProviderNetworkError("connection reset")
        )
        await extract_content(trigger=_trigger(), ctx=ctx)
    mock_audit.assert_awaited_once()


# ---------------------------------------------------------------------------
# RetryableError + INLINE_RETRY=False — 即 audit + return (retry しない)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_cls",
    [
        AIProviderRateLimitedError,
        AIProviderQuotaExhaustedError,
    ],
)
@pytest.mark.asyncio
async def test_retryable_inline_false_audits_immediately(
    exc_cls: type[Exception],
) -> None:
    """INLINE_RETRY=False の RetryableError は retry せず即 audit + return。"""
    from app.analysis.extraction.tasks import extract_content

    ctx = _make_ctx(retry_count=0, max_retries=1)  # retry 余地ありでも raise しない

    with (
        _patch_try_advance_from(),
        patch(
            "app.analysis.extraction.tasks._build_limiters", return_value=(None, None)
        ),
        patch("app.analysis.extraction.tasks.ExtractionService") as mock_svc_cls,
        patch(
            "app.analysis.extraction.tasks._record_failure",
            new=AsyncMock(),
        ) as mock_audit,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc_cls("boom"))
        await extract_content(trigger=_trigger(), ctx=ctx)
    mock_audit.assert_awaited_once()


# ---------------------------------------------------------------------------
# catch-all — 想定外例外は audit + return (UNKNOWN ラベル)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unexpected_exception_falls_through_to_catch_all() -> None:
    """Layer 1 marker いずれにも該当しない exc は catch-all で audit + return。"""
    from app.analysis.extraction.tasks import extract_content

    ctx = _make_ctx()
    with (
        _patch_try_advance_from(),
        patch(
            "app.analysis.extraction.tasks._build_limiters", return_value=(None, None)
        ),
        patch("app.analysis.extraction.tasks.ExtractionService") as mock_svc_cls,
        patch(
            "app.analysis.extraction.tasks._record_failure",
            new=AsyncMock(),
        ) as mock_audit,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(
            side_effect=ValueError("surprise"),
        )
        await extract_content(trigger=_trigger(), ctx=ctx)
    mock_audit.assert_awaited_once()
    assert isinstance(mock_audit.await_args.kwargs["exc"], ValueError)
