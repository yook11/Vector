"""Briefing error class の監査属性テスト。"""

from __future__ import annotations

from app.audit.domain.event import Stage
from app.audit.failure_projection import Retryability
from app.insights.briefing.errors import (
    BriefingConfigurationError,
    BriefingError,
    BriefingLlmError,
    BriefingLlmResponseInvalidError,
)


def test_briefing_llm_configuration_invalid_classvars_are_ssot() -> None:
    assert BriefingError.STAGE is Stage.BRIEFING
    assert (
        BriefingConfigurationError.CODE
        == "briefing_generation_llm_configuration_invalid"
    )
    assert BriefingConfigurationError.FAILURE_KIND == "configuration"
    assert BriefingConfigurationError.RETRYABILITY is Retryability.NON_RETRYABLE
    assert BriefingConfigurationError.FAILURE_ACTION is None


def test_briefing_llm_provider_call_failed_classvars_are_ssot() -> None:
    provider_error = RuntimeError("upstream")
    exc = BriefingLlmError(provider_error=provider_error)

    assert exc.STAGE is Stage.BRIEFING
    assert exc.CODE == "briefing_generation_llm_provider_call_failed"
    assert exc.FAILURE_KIND == "llm_error"
    assert exc.RETRYABILITY is Retryability.RETRYABLE
    assert exc.FAILURE_ACTION is None
    assert exc.provider_error is provider_error


def test_briefing_llm_response_contract_invalid_classvars_are_ssot() -> None:
    exc = BriefingLlmResponseInvalidError()

    assert exc.STAGE is Stage.BRIEFING
    assert exc.CODE == "briefing_generation_llm_response_contract_invalid"
    assert exc.FAILURE_KIND == "response_invalid"
    assert exc.RETRYABILITY is Retryability.NON_RETRYABLE
    assert exc.FAILURE_ACTION is None


def test_briefing_llm_response_invalid_no_violations_str_is_code_only() -> None:
    """violations なし (引数なし構築) は str(exc) が CODE のみ。後方互換保証。"""
    exc = BriefingLlmResponseInvalidError()
    assert str(exc) == BriefingLlmResponseInvalidError.CODE


def test_briefing_llm_response_invalid_with_violations_appends_detail() -> None:
    """violations ありの str(exc) は CODE + ': ' + violations をセミコロン結合。"""
    exc = BriefingLlmResponseInvalidError(violations=("headline: string_too_long",))
    expected = (
        "briefing_generation_llm_response_contract_invalid: headline: string_too_long"
    )
    assert str(exc) == expected


def test_briefing_llm_response_invalid_multiple_violations_are_semicolon_joined() -> (
    None
):
    """複数 violations はセミコロン + スペースで結合されて str(exc) に現れる。"""
    exc = BriefingLlmResponseInvalidError(
        violations=("key_articles.0: value_error", "summary: string_too_long")
    )
    msg = str(exc)
    assert "key_articles.0: value_error" in msg
    assert "summary: string_too_long" in msg
    assert "; " in msg


def test_briefing_llm_response_invalid_violations_attribute_is_tuple() -> None:
    """violations 属性は渡したシーケンスが tuple に変換されて保持される。"""
    exc = BriefingLlmResponseInvalidError(
        violations=["headline: string_too_long", "summary: string_too_long"]
    )
    assert exc.violations == (
        "headline: string_too_long",
        "summary: string_too_long",
    )
