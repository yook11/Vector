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

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderNetworkError,
    AIProviderRateLimitedError,
)
from app.analysis.assessment.ai.base import BaseAssessor
from app.analysis.assessment.ai.envelope import AssessmentCall
from app.analysis.assessment.domain.result import InScope, OutOfScope
from app.analysis.assessment.errors import (
    AssessmentRecoverableError,
    AssessmentResponseInvalidError,
    AssessmentTerminalStageBlockedError,
)
from app.analysis.rate_limit import AIModelRateLimitPolicy


class _StubAssessor(BaseAssessor):
    """テスト用の最小 BaseAssessor 派生 (abstract method を mock で差し替える)。"""

    @property
    def model_name(self) -> str:
        return "test-model"

    @property
    def prompt_version(self) -> str:
        return "abc12345"

    @property
    def rate_limit_policy(self) -> AIModelRateLimitPolicy:
        return AIModelRateLimitPolicy(provider="test", model="test-model", rules=())

    def __init__(self) -> None:
        # client 不要 (mock で _call_api を差し替えるため)
        pass

    async def assess(  # pragma: no cover - 直接テストしない
        self, title_ja: str, summary_ja: str
    ) -> AssessmentCall[InScope] | AssessmentCall[OutOfScope]:
        return await self._call_once("p")

    async def _call_api(  # pragma: no cover - mock で override
        self, prompt: str
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


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


class TestCallOnceSuccess:
    """正常系: ``_call_api`` の戻り値を素通しで返す。"""

    @pytest.mark.asyncio
    async def test_returns_assessment_call(self) -> None:
        cls = _StubAssessor()
        cls._call_api = AsyncMock(return_value=_make_call())  # type: ignore[method-assign]
        result = await cls._call_once("prompt")
        assert isinstance(result, AssessmentCall)


# ---------------------------------------------------------------------------
# Passthrough: AIProviderError / AssessmentError は translate を経由しない
# ---------------------------------------------------------------------------


class TestCallOncePassthrough:
    """AIProviderError / AssessmentError は _translate_error を経由せず素通し。"""

    @pytest.mark.asyncio
    async def test_ai_provider_rate_limited_passes_through_unchanged(self) -> None:
        original = AIProviderRateLimitedError("rate limited")
        cls = _StubAssessor()
        cls._call_api = AsyncMock(side_effect=original)  # type: ignore[method-assign]
        cls._translate_error = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("must not be called")
        )

        with pytest.raises(AIProviderRateLimitedError) as exc_info:
            await cls._call_once("prompt")

        assert exc_info.value is original
        assert exc_info.value.__cause__ is None  # ラップしない

    @pytest.mark.asyncio
    async def test_ai_provider_configuration_passes_through_unchanged(self) -> None:
        original = AIProviderConfigurationError("bad api key")
        cls = _StubAssessor()
        cls._call_api = AsyncMock(side_effect=original)  # type: ignore[method-assign]
        cls._translate_error = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("must not be called")
        )

        with pytest.raises(AIProviderConfigurationError) as exc_info:
            await cls._call_once("prompt")

        assert exc_info.value is original

    @pytest.mark.asyncio
    async def test_assessment_response_invalid_passes_through_unchanged(self) -> None:
        original = AssessmentResponseInvalidError()
        cls = _StubAssessor()
        cls._call_api = AsyncMock(side_effect=original)  # type: ignore[method-assign]
        cls._translate_error = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("must not be called")
        )

        with pytest.raises(AssessmentResponseInvalidError) as exc_info:
            await cls._call_once("prompt")

        assert exc_info.value is original

    @pytest.mark.asyncio
    async def test_assessment_recoverable_base_passes_through(self) -> None:
        original = AssessmentRecoverableError(code="z")
        cls = _StubAssessor()
        cls._call_api = AsyncMock(side_effect=original)  # type: ignore[method-assign]
        cls._translate_error = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("must not be called")
        )

        with pytest.raises(AssessmentRecoverableError) as exc_info:
            await cls._call_once("prompt")
        assert exc_info.value is original

    @pytest.mark.asyncio
    async def test_assessment_terminal_stage_blocked_base_passes_through(self) -> None:
        original = AssessmentTerminalStageBlockedError(code="z")
        cls = _StubAssessor()
        cls._call_api = AsyncMock(side_effect=original)  # type: ignore[method-assign]
        cls._translate_error = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("must not be called")
        )

        with pytest.raises(AssessmentTerminalStageBlockedError) as exc_info:
            await cls._call_once("prompt")
        assert exc_info.value is original


# ---------------------------------------------------------------------------
# Translate path: マップ可能なら from exc 連鎖、未知ならそのまま素通し
# ---------------------------------------------------------------------------


class TestCallOnceTranslate:
    """``_translate_error`` 経由のマップ / 未知の処理。"""

    @pytest.mark.asyncio
    async def test_translatable_exception_wrapped_with_from(self) -> None:
        original = ConnectionError("network down")
        translated = AIProviderNetworkError("translated")
        cls = _StubAssessor()
        cls._call_api = AsyncMock(side_effect=original)  # type: ignore[method-assign]
        cls._translate_error = MagicMock(return_value=translated)  # type: ignore[method-assign]

        with pytest.raises(AIProviderNetworkError) as exc_info:
            await cls._call_once("prompt")

        assert exc_info.value is translated
        # `from exc` で原因連鎖
        assert exc_info.value.__cause__ is original

    @pytest.mark.asyncio
    async def test_unmappable_exception_bare_reraise(self) -> None:
        # _translate_error が exc をそのまま return → from なしで素通し
        original = RuntimeError("unmappable")
        cls = _StubAssessor()
        cls._call_api = AsyncMock(side_effect=original)  # type: ignore[method-assign]
        cls._translate_error = MagicMock(return_value=original)  # type: ignore[method-assign]

        with pytest.raises(RuntimeError) as exc_info:
            await cls._call_once("prompt")

        assert exc_info.value is original
        # bare re-raise: from を付けないので __cause__ は None
        assert exc_info.value.__cause__ is None
