"""Geminiの応答から、ブロックと打ち切りを判定し、頼んだ結果を取り出す。"""

from __future__ import annotations

import json
from typing import Any

from app.ai_providers.errors import AIProviderResultError, AIProviderResultReason
from app.ai_providers.gemini.error_translator import (
    OUTPUT_BLOCKED_FINISH_REASONS,
    output_blocked_reason,
)

__all__ = ["GeminiStreamReader", "read_gemini_json_object"]


def read_gemini_json_object(response: object) -> dict[str, Any]:
    """ブロックと打ち切りを失敗にし、応答の本文をJSON objectとして返す。"""
    _raise_for_blocked_or_truncated(response, _finish_reason_names(response))
    try:
        payload = json.loads(getattr(response, "text", None) or "")
    except (json.JSONDecodeError, TypeError):
        payload = None
    if isinstance(payload, dict):
        return payload
    raise AIProviderResultError(reason=AIProviderResultReason.RESPONSE_UNPARSEABLE)


class GeminiStreamReader:
    """streamの断片を順に読み、ブロック・打ち切り・終わりの欠けを失敗にする。"""

    __slots__ = ("_finished",)

    def __init__(self) -> None:
        self._finished = False

    def read(self, chunk: object) -> str | None:
        """ブロックか打ち切りなら失敗にし、断片の本文を返す。"""
        finish_reason_names = _finish_reason_names(chunk)
        _raise_for_blocked_or_truncated(chunk, finish_reason_names)
        self._finished = self._finished or bool(finish_reason_names)
        return getattr(chunk, "text", None)

    def finish(self) -> None:
        """終わりの理由を受け取らずにstreamが尽きたら、途切れた失敗にする。"""
        if not self._finished:
            raise AIProviderResultError(reason=AIProviderResultReason.STREAM_INCOMPLETE)


def _raise_for_blocked_or_truncated(
    response: object, finish_reason_names: list[str]
) -> None:
    if _has_prompt_block(response):
        raise AIProviderResultError(reason=AIProviderResultReason.INPUT_BLOCKED)
    blocked_reason_name = next(
        (name for name in finish_reason_names if name in OUTPUT_BLOCKED_FINISH_REASONS),
        None,
    )
    if blocked_reason_name is not None:
        raise AIProviderResultError(reason=output_blocked_reason(blocked_reason_name))
    if "MAX_TOKENS" in finish_reason_names:
        raise AIProviderResultError(reason=AIProviderResultReason.OUTPUT_TRUNCATED)


def _has_prompt_block(response: object) -> bool:
    prompt_feedback = getattr(response, "prompt_feedback", None)
    return (
        prompt_feedback is not None
        and getattr(prompt_feedback, "block_reason", None) is not None
    )


def _finish_reason_names(response: object) -> list[str]:
    names: list[str] = []
    for candidate in getattr(response, "candidates", None) or []:
        finish_reason = getattr(candidate, "finish_reason", None)
        if finish_reason is None:
            continue
        if isinstance(finish_reason, str):
            names.append(finish_reason)
            continue
        name = getattr(finish_reason, "name", None)
        if isinstance(name, str) and name:
            names.append(name)
            continue
        value = getattr(finish_reason, "value", None)
        if isinstance(value, str) and value:
            names.append(value)
    return names
