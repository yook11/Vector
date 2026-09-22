"""``BaseAssessor._call_once`` の bare re-raise guard パターンのテスト。

PR3 で導入した:
- ``(AIProviderError, AssessmentError)`` の素通し (二重翻訳防止)
- ``_translate_error`` 経由でマップ済み例外は ``raise translated from exc``
- マップ未知 (``_translate_error`` が exc をそのまま return) は ``raise``
  (from なし、bare re-raise)

を検証する。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from app.ai_providers.errors import (
    AIProviderConfigurationError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
)
from app.analysis.assessment.ai.base import BaseAssessor
from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.ai.parse import AssessmentResponseDefect
from app.analysis.assessment.domain.result import InScope, OutOfScope
from app.analysis.assessment.errors import (
    AssessmentError,
    AssessmentResponseInvalidError,
    to_assessment_error,
)


class _StubAssessor(BaseAssessor):
    """テスト用の最小 BaseAssessor 派生 (abstract method を mock で差し替える)。"""

    @property
    def model_name(self) -> str:
        return "test-model"

    @property
    def prompt_version(self) -> str:
        return "abc12345"

    @property
    def provider(self) -> str:
        return "test"

    def __init__(self) -> None:
        # client 不要 (mock で _call_api を差し替えるため)
        pass

    async def assess(
        self, title_ja: str, summary_ja: str, *, logger
    ) -> AssessmentCall[InScope] | AssessmentCall[OutOfScope]:
        return await self._call_once("p", logger=logger)

    async def _call_api(  # pragma: no cover - mock で override
        self, prompt: str, *, logger
    ) -> AssessmentCall[InScope] | AssessmentCall[OutOfScope]:
        raise NotImplementedError

    def _translate_error(  # pragma: no cover - mock で override
        self, exc: Exception
    ) -> Exception:
        return exc


def _make_call() -> AssessmentCall[OutOfScope]:
    return AssessmentCall(
        result=OutOfScope(investor_take="x"),
        raw_response='{"category": "out_of_scope", "investor_take": "x"}',
        raw_category="out_of_scope",
        prompt_version="abc12345",
        model_name="test-model",
    )


class TestCallOnceSuccess:
    """正常系: ``_call_api`` の戻り値を素通しで返す。"""

    @pytest.mark.asyncio
    async def test_returns_assessment_call(self, make_assessment_logger) -> None:
        cls = _StubAssessor()
        cls._call_api = AsyncMock(return_value=_make_call())  # type: ignore[method-assign]
        result = await cls._call_once("prompt", logger=make_assessment_logger())
        assert isinstance(result, AssessmentCall)


class TestCallOncePassthrough:
    """AIProviderError / AssessmentError は _translate_error を経由せず素通し。"""

    @pytest.mark.asyncio
    async def test_ai_provider_rate_limited_passes_through_unchanged(
        self, make_assessment_logger
    ) -> None:
        original = AIProviderRateLimitedError("rate limited")
        cls = _StubAssessor()
        cls._call_api = AsyncMock(side_effect=original)  # type: ignore[method-assign]
        cls._translate_error = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("must not be called")
        )

        with pytest.raises(AIProviderRateLimitedError) as exc_info:
            await cls._call_once("prompt", logger=make_assessment_logger())

        assert exc_info.value is original
        assert exc_info.value.__cause__ is None

    @pytest.mark.asyncio
    async def test_ai_provider_configuration_passes_through_unchanged(
        self, make_assessment_logger
    ) -> None:
        original = AIProviderConfigurationError("bad api key")
        cls = _StubAssessor()
        cls._call_api = AsyncMock(side_effect=original)  # type: ignore[method-assign]
        cls._translate_error = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("must not be called")
        )

        with pytest.raises(AIProviderConfigurationError) as exc_info:
            await cls._call_once("prompt", logger=make_assessment_logger())

        assert exc_info.value is original

    @pytest.mark.asyncio
    async def test_assessment_response_invalid_passes_through_unchanged(
        self, make_assessment_logger
    ) -> None:
        original = AssessmentResponseInvalidError(
            AssessmentResponseDefect.CATEGORY_KEY_MISSING
        )
        cls = _StubAssessor()
        cls._call_api = AsyncMock(side_effect=original)  # type: ignore[method-assign]
        cls._translate_error = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("must not be called")
        )

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await cls._call_once("prompt", logger=make_assessment_logger())

        assert exc_info.value is original

    @pytest.mark.asyncio
    async def test_assessment_network_error_passes_through(
        self, make_assessment_logger
    ) -> None:
        original = to_assessment_error(AIProviderNetworkError())
        cls = _StubAssessor()
        cls._call_api = AsyncMock(side_effect=original)  # type: ignore[method-assign]
        cls._translate_error = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("must not be called")
        )

        with pytest.raises(AssessmentError) as exc_info:
            await cls._call_once("prompt", logger=make_assessment_logger())
        assert exc_info.value is original

    @pytest.mark.asyncio
    async def test_assessment_configuration_error_passes_through(
        self, make_assessment_logger
    ) -> None:
        original = to_assessment_error(AIProviderConfigurationError())
        cls = _StubAssessor()
        cls._call_api = AsyncMock(side_effect=original)  # type: ignore[method-assign]
        cls._translate_error = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("must not be called")
        )

        with pytest.raises(AssessmentError) as exc_info:
            await cls._call_once("prompt", logger=make_assessment_logger())
        assert exc_info.value is original


class TestCallOnceTranslate:
    """``_translate_error`` 経由のマップ / 未知の処理。"""

    @pytest.mark.asyncio
    async def test_translatable_exception_wrapped_with_from(
        self, make_assessment_logger
    ) -> None:
        original = ConnectionError("network down")
        translated = AIProviderNetworkError("translated")
        cls = _StubAssessor()
        cls._call_api = AsyncMock(side_effect=original)  # type: ignore[method-assign]
        cls._translate_error = MagicMock(return_value=translated)  # type: ignore[method-assign]

        with pytest.raises(AIProviderNetworkError) as exc_info:
            await cls._call_once("prompt", logger=make_assessment_logger())

        assert exc_info.value is translated
        # `from exc` で原因連鎖
        assert exc_info.value.__cause__ is original

    @pytest.mark.asyncio
    async def test_unmappable_exception_bare_reraise(
        self, make_assessment_logger
    ) -> None:
        # _translate_error が exc をそのまま return → from なしで素通し
        original = RuntimeError("unmappable")
        cls = _StubAssessor()
        cls._call_api = AsyncMock(side_effect=original)  # type: ignore[method-assign]
        cls._translate_error = MagicMock(return_value=original)  # type: ignore[method-assign]

        with pytest.raises(RuntimeError) as exc_info:
            await cls._call_once("prompt", logger=make_assessment_logger())

        assert exc_info.value is original
        # bare re-raise: from を付けないので __cause__ は None
        assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_message_logger_reaches_api_with_model(make_assessment_logger):
    """メッセージの相関情報を維持してモデルを加え、AI呼び出しへ渡す。"""
    message_logger = make_assessment_logger()
    assessor = _StubAssessor()
    assessor._call_api = AsyncMock(return_value=_make_call())
    await assessor.assess("title", "summary", logger=message_logger)
    passed_logger = assessor._call_api.await_args.kwargs["logger"]
    context = structlog.get_context(passed_logger)
    assert context["request_id"] == "request-001"
    assert context["message_id"] == "message-001"
    assert context["model"] == "test-model"
    assert "model" not in structlog.get_context(message_logger)
