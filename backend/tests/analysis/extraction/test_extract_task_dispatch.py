"""``extract_content`` task の例外 dispatch routing テスト (PR3-a-1)。

Service の execute を mock して、tasks.py が **どの例外** を **どの outcome_code**
に振り分け、**audit のみ / DELETE / inline retry** のいずれを取るかを検証する。

実 DB / 実 Service / 実 _record_failure_event は呼ばない:
- ``ExtractionService`` を patch
- ``_audit_extraction_failure`` を patch (内部で _record_failure_event を呼ぶ)
- ``mark_article_unprocessable`` を patch
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.analysis.errors import (
    ConfigurationError,
    DailyQuotaExhaustedError,
    InsufficientBalanceError,
    NetworkError,
    ProviderError,
    UnclassifiedError,
)
from app.analysis.errors import RateLimitError as AnalysisRateLimitError
from app.analysis.extraction.domain.ready import ReadyForExtraction
from app.analysis.extraction.extractor.errors import (
    ExtractionInputTooLargeError,
    ExtractionPolicyBlockedError,
)
from app.analysis.extraction.extractor.gemini_prompt import GeminiExtractionPrompt


def _make_provider_fake() -> MagicMock:
    fake = MagicMock()
    fake.MODEL = "test-model"
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


def _ready() -> ReadyForExtraction:
    return ReadyForExtraction(
        article_id=42, original_title="t", original_content="content body"
    )


# ---------------------------------------------------------------------------
# DELETE 経路 (内容起因 Permanent)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_policy_blocked_calls_mark_unprocessable() -> None:
    from app.analysis.tasks import extract_content

    ctx = _make_ctx()
    exc = ExtractionPolicyBlockedError(
        finish_reason="SAFETY",
        raw_response="x",
        prompt_version=GeminiExtractionPrompt.VERSION,
    )

    with (
        patch("app.analysis.tasks._build_limiters", return_value=(None, None)),
        patch("app.analysis.tasks.ExtractionService") as mock_svc_cls,
    ):
        svc_instance = mock_svc_cls.return_value
        svc_instance.execute = AsyncMock(side_effect=exc)
        svc_instance.mark_article_unprocessable = AsyncMock()
        await extract_content(ready=_ready(), ctx=ctx)
        svc_instance.mark_article_unprocessable.assert_awaited_once()
        kwargs = svc_instance.mark_article_unprocessable.await_args.kwargs
        args = svc_instance.mark_article_unprocessable.await_args.args
        assert args[0] == 42  # article_id
        assert kwargs["outcome_code"] == "ai_error_blocked_by_policy"
        assert kwargs["exc"] is exc


@pytest.mark.asyncio
async def test_input_too_large_calls_mark_unprocessable() -> None:
    from app.analysis.tasks import extract_content

    ctx = _make_ctx()
    exc = ExtractionInputTooLargeError(prompt_version=GeminiExtractionPrompt.VERSION)

    with (
        patch("app.analysis.tasks._build_limiters", return_value=(None, None)),
        patch("app.analysis.tasks.ExtractionService") as mock_svc_cls,
    ):
        svc_instance = mock_svc_cls.return_value
        svc_instance.execute = AsyncMock(side_effect=exc)
        svc_instance.mark_article_unprocessable = AsyncMock()
        await extract_content(ready=_ready(), ctx=ctx)
        svc_instance.mark_article_unprocessable.assert_awaited_once()
        kwargs = svc_instance.mark_article_unprocessable.await_args.kwargs
        assert kwargs["outcome_code"] == "ai_error_input_too_large"


# ---------------------------------------------------------------------------
# 環境起因 Permanent (記事保持、人間対応)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc_cls", "expected_code"),
    [
        (ConfigurationError, "ai_error_config"),
        (InsufficientBalanceError, "ai_error_insufficient_balance"),
        (DailyQuotaExhaustedError, "ai_error_daily_quota_exhausted"),
        (AnalysisRateLimitError, "ai_error_rate_limited"),
        (UnclassifiedError, "unclassified_error"),
    ],
)
@pytest.mark.asyncio
async def test_non_retry_failures_audit_with_correct_outcome_code(
    exc_cls: type[Exception], expected_code: str
) -> None:
    from app.analysis.tasks import extract_content

    ctx = _make_ctx()
    with (
        patch("app.analysis.tasks._build_limiters", return_value=(None, None)),
        patch("app.analysis.tasks.ExtractionService") as mock_svc_cls,
        patch(
            "app.analysis.tasks._audit_extraction_failure",
            new=AsyncMock(),
        ) as mock_audit,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=exc_cls("boom"))
        await extract_content(ready=_ready(), ctx=ctx)
    mock_audit.assert_awaited_once()
    assert mock_audit.await_args.kwargs["outcome_code"] == expected_code


# ---------------------------------------------------------------------------
# inline retry 対象 (NetworkError / ProviderError)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_network_error_raises_when_not_last_attempt() -> None:
    from app.analysis.tasks import extract_content

    ctx = _make_ctx(retry_count=0, max_retries=1)  # retry 余地あり

    with (
        patch("app.analysis.tasks._build_limiters", return_value=(None, None)),
        patch("app.analysis.tasks.ExtractionService") as mock_svc_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(
            side_effect=NetworkError("connection reset")
        )
        with pytest.raises(NetworkError):
            await extract_content(ready=_ready(), ctx=ctx)


@pytest.mark.asyncio
async def test_network_error_audits_on_last_attempt() -> None:
    from app.analysis.tasks import extract_content

    ctx = _make_ctx(retry_count=1, max_retries=1)  # 最終試行

    with (
        patch("app.analysis.tasks._build_limiters", return_value=(None, None)),
        patch("app.analysis.tasks.ExtractionService") as mock_svc_cls,
        patch(
            "app.analysis.tasks._audit_extraction_failure",
            new=AsyncMock(),
        ) as mock_audit,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(
            side_effect=NetworkError("connection reset")
        )
        await extract_content(ready=_ready(), ctx=ctx)
    mock_audit.assert_awaited_once()
    assert mock_audit.await_args.kwargs["outcome_code"] == "ai_error_network"


@pytest.mark.asyncio
async def test_provider_error_raises_on_retry_audits_on_last_attempt() -> None:
    from app.analysis.tasks import extract_content

    # not last attempt → raise
    ctx = _make_ctx(retry_count=0, max_retries=1)
    with (
        patch("app.analysis.tasks._build_limiters", return_value=(None, None)),
        patch("app.analysis.tasks.ExtractionService") as mock_svc_cls,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=ProviderError("5xx"))
        with pytest.raises(ProviderError):
            await extract_content(ready=_ready(), ctx=ctx)

    # last attempt → audit
    ctx = _make_ctx(retry_count=1, max_retries=1)
    with (
        patch("app.analysis.tasks._build_limiters", return_value=(None, None)),
        patch("app.analysis.tasks.ExtractionService") as mock_svc_cls,
        patch(
            "app.analysis.tasks._audit_extraction_failure",
            new=AsyncMock(),
        ) as mock_audit,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(side_effect=ProviderError("5xx"))
        await extract_content(ready=_ready(), ctx=ctx)
    assert mock_audit.await_args.kwargs["outcome_code"] == "ai_error_provider"


# ---------------------------------------------------------------------------
# 想定外例外
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unexpected_exception_audits_with_unexpected_error_code() -> None:
    from app.analysis.tasks import extract_content

    ctx = _make_ctx()
    with (
        patch("app.analysis.tasks._build_limiters", return_value=(None, None)),
        patch("app.analysis.tasks.ExtractionService") as mock_svc_cls,
        patch(
            "app.analysis.tasks._audit_extraction_failure",
            new=AsyncMock(),
        ) as mock_audit,
    ):
        mock_svc_cls.return_value.execute = AsyncMock(
            side_effect=ValueError("surprise"),
        )
        await extract_content(ready=_ready(), ctx=ctx)
    assert mock_audit.await_args.kwargs["outcome_code"] == "unexpected_error"
