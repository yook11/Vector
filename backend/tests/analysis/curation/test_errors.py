"""Curationの失敗理由と原因保持の契約を検証する。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai_providers.errors import AIProviderError, AIProviderRateLimitedError
from app.analysis.curation.domain.ready import ReadyForCuration
from app.analysis.curation.errors import (
    CurationError,
    CurationFailureReason,
    CurationResponseInvalidError,
    to_curation_error,
)
from app.analysis.curation.service import CurationService
from app.analysis.curation.task_errors import (
    CurationRecoverableError,
    to_curation_task_error,
)


@pytest.mark.parametrize(
    "reason, provider",
    [
        ("provider_error", None),
        (CurationFailureReason.PROVIDER_ERROR, None),
        (CurationFailureReason.PROVIDER_ERROR, AIProviderError()),
        (CurationFailureReason.RESPONSE_INVALID, AIProviderRateLimitedError()),
    ],
)
def test_invalid_error_combinations_are_rejected(reason, provider):
    """原因と失敗理由の矛盾を構築時に拒否する。"""
    with pytest.raises(TypeError):
        CurationError(reason=reason, provider_error=provider)


def test_response_invalid_keeps_code_without_legacy_policy():
    """応答不正は既存コードを保ち、旧経路の制御属性を持たない。"""
    error = CurationResponseInvalidError()
    assert error.reason is CurationFailureReason.RESPONSE_INVALID
    assert error.code == "extraction_response_invalid"
    assert error.provider_error is None
    assert (
        str(error) == "CurationResponseInvalidError(code='extraction_response_invalid')"
    )
    assert not hasattr(error, "RETRYABILITY")
    assert not hasattr(error, "FAILURE_ACTION")
    assert not isinstance(error, CurationRecoverableError)


def test_provider_error_string_does_not_expose_provider_message():
    """Serviceエラーの文字列表現へプロバイダーの自由文を出さない。"""
    provider = AIProviderRateLimitedError("private provider details")
    error = to_curation_error(provider)
    assert error.provider_error is provider
    assert error.reason is CurationFailureReason.PROVIDER_ERROR
    assert error.code == provider.CODE
    assert "private" not in str(error)


def test_response_invalid_maps_to_existing_task_policy_with_cause():
    """応答不正は旧経路で再試行分類となり、業務上の原因を保持する。"""
    original = CurationResponseInvalidError()
    mapped = to_curation_task_error(original)
    assert isinstance(mapped, CurationRecoverableError)
    assert mapped.code == "extraction_response_invalid"
    assert mapped.failure_kind == "ai_response_invalid"
    assert mapped.provider_error is None
    assert mapped.failure_reason is None
    assert mapped.__cause__ is original


@pytest.mark.parametrize(
    "original", [RuntimeError("unexpected"), asyncio.CancelledError()]
)
def test_non_curation_errors_are_not_reclassified(original):
    """Curation以外の例外は旧経路への変換でも同じインスタンスを保つ。"""
    assert to_curation_task_error(original) is original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original",
    [
        CurationResponseInvalidError(),
        RuntimeError("unexpected"),
        TimeoutError(),
        asyncio.CancelledError(),
    ],
)
async def test_service_propagates_non_provider_errors_without_opening_database(
    original,
):
    """AI呼び出しの非プロバイダー例外を変換せず、DBを開かずに伝播する。"""
    session_factory = MagicMock()
    curator = MagicMock()
    curator.curate = AsyncMock(side_effect=original)
    ready = ReadyForCuration(
        analyzable_article_id=42, original_title="title", original_content="body"
    )
    with pytest.raises(type(original)) as raised:
        await CurationService(session_factory).execute(ready, curator)
    assert raised.value is original
    session_factory.assert_not_called()


@pytest.mark.asyncio
async def test_service_wraps_provider_with_same_cause():
    """プロバイダー例外を属性と原因チェーンの両方で保持する。"""
    provider = AIProviderRateLimitedError("private provider details")
    curator = MagicMock()
    curator.curate = AsyncMock(side_effect=provider)
    session_factory = MagicMock()
    ready = ReadyForCuration(
        analyzable_article_id=42, original_title="title", original_content="body"
    )
    with pytest.raises(CurationError) as raised:
        await CurationService(session_factory).execute(ready, curator)
    assert raised.value.provider_error is provider
    assert raised.value.__cause__ is provider
    session_factory.assert_not_called()
