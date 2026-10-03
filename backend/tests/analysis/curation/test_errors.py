"""Curationの失敗理由と、Serviceが例外を変換せずに伝える契約を検証する。"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai_providers.errors import (
    AIProviderResponseError,
    AIProviderResponseReason,
)
from app.analysis.curation.domain.ready import ReadyForCuration
from app.analysis.curation.errors import (
    CurationError,
    CurationFailureReason,
    CurationResponseInvalidError,
)
from app.analysis.curation.service import CurationService
from app.http.errors import HttpResponseError

_RECEIVED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def test_untyped_reason_is_rejected():
    """失敗理由はenumの値だけを受け付ける。"""
    with pytest.raises(TypeError):
        CurationError(reason="response_invalid")


def test_response_invalid_keeps_code_without_legacy_policy():
    """応答不正は既存コードを保ち、旧経路の制御属性を持たない。"""
    error = CurationResponseInvalidError()
    assert error.reason is CurationFailureReason.RESPONSE_INVALID
    assert error.code == "extraction_response_invalid"
    assert not hasattr(error, "RETRYABILITY")
    assert not hasattr(error, "FAILURE_ACTION")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original",
    [
        AIProviderResponseError(
            reason=AIProviderResponseReason.RATE_LIMITED,
            http_error=HttpResponseError(status_code=429, received_at=_RECEIVED_AT),
        ),
        CurationResponseInvalidError(),
        RuntimeError("unexpected"),
        TimeoutError(),
        asyncio.CancelledError(),
    ],
)
async def test_service_propagates_ai_call_errors_without_opening_database(original):
    """AI呼び出しの例外はAIの失敗も含めて変換せず、DBを開かずに伝播する。"""
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


def test_curation_error_directly_inherits_exception():
    """工程例外はログ固有の基底クラスへ依存しない。"""
    assert CurationError.__bases__ == (Exception,)


def test_curation_error_uses_standard_empty_message():
    """メッセージ未指定の例外文字列をcodeで補完しない。"""
    error = CurationResponseInvalidError()
    assert error.args == ()
    assert str(error) == ""


def test_response_invalid_is_curation_error():
    """応答不正を工程例外として捕捉できる。"""
    assert issubclass(CurationResponseInvalidError, CurationError)


def test_response_invalid_rejects_positional_message():
    """応答不正の既存の引数なしコンストラクターを維持する。"""
    with pytest.raises(TypeError):
        CurationResponseInvalidError("diagnostic")
