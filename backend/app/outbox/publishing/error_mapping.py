"""通信に依存しない本文準備の失敗を配送の失敗契約へ変換する。"""

from app.outbox.publishing.errors import (
    PublishError,
    PublishPhase,
    PublishUnexpectedError,
)


def publish_preparation_error_from_exception(exc: Exception) -> PublishError:
    """分類済みの失敗は維持し、想定外の例外には本文準備の段階と原因を付ける。"""
    if isinstance(exc, PublishError):
        return exc
    error = PublishUnexpectedError(
        original_exception=exc, phase=PublishPhase.PREPARE_EVENT
    )
    error.__cause__ = exc
    return error
