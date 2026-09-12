"""本文準備の失敗をSQSに依存せず同じ配送契約へ変換することを確認する。"""

import pytest

from app.outbox.publishing.error_mapping import publish_preparation_error_from_exception
from app.outbox.publishing.errors import (
    PublishEventInvalidError,
    PublishEventInvalidReason,
    PublishPhase,
    PublishUnexpectedError,
)

pytestmark = pytest.mark.unit


def test_classified_preparation_error_keeps_identity_and_cause():
    """分類済みの本文準備エラーは、詳細と原因を持つ同一の例外として返す。"""
    original = PublishEventInvalidError(
        reason=PublishEventInvalidReason.INVALID_PAYLOAD
    )
    cause = ValueError("PRIVATE")
    original.__cause__ = cause

    error = publish_preparation_error_from_exception(original)

    assert error is original
    assert error.__cause__ is cause


def test_unexpected_preparation_error_keeps_phase_and_private_cause():
    """想定外の本文準備例外は段階・原因を維持し、自由文をエラー文字列へ出さない。"""
    original = ValueError("PRIVATE_BODY_QUEUE_CREDENTIAL")

    error = publish_preparation_error_from_exception(original)

    assert isinstance(error, PublishUnexpectedError)
    assert vars(error) == {
        "original_exception_type": "builtins.ValueError",
        "phase": PublishPhase.PREPARE_EVENT,
        "classification_exception_type": None,
    }
    assert error.__cause__ is original
    assert "PRIVATE" not in str(error)
    assert "PRIVATE" not in repr(error)
