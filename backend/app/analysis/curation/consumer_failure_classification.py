"""Consumerの失敗から、再配信に任せるか受信完了にするかを決める純粋関数。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from app.ai_providers.errors import AIProviderError
from app.analysis.ai_provider_retry import is_unrecoverable_for_input
from app.analysis.curation.domain.ready import CurationReadyBuildRejected
from app.analysis.curation.errors import CurationError, CurationFailureReason


@dataclass(frozen=True, slots=True)
class RetryCuration:
    """SQS の再配信に任せる Curation の失敗。"""

    error: Exception


@dataclass(frozen=True, slots=True)
class NoRetryCuration:
    """再配信しても変わらないため受信完了にする Curation の失敗。"""

    cause: CurationReadyBuildRejected | AIProviderError


def classify_curation_failure(exc: Exception) -> RetryCuration | NoRetryCuration:
    """同じ入力では変わらない失敗だけを受信完了にし、DB障害・想定外は再配信する。"""
    if isinstance(exc, AIProviderError):
        if is_unrecoverable_for_input(exc):
            return NoRetryCuration(exc)
        return RetryCuration(exc)
    if isinstance(exc, CurationError):
        match exc.reason:
            case CurationFailureReason.RESPONSE_INVALID:
                return RetryCuration(exc)
            case _:
                assert_never(exc.reason)
    return RetryCuration(exc)
