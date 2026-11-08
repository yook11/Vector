"""Provider-neutralなstructured outputのschema変換とJSON objectの読み取り。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from app.ai_providers.errors import AIProviderResultError, AIProviderResultReason


def thaw_schema(value: Any) -> Any:
    """Agentの凍結schemaをprovider request用のJSON互換値へ戻す。"""
    if isinstance(value, Mapping):
        return {key: thaw_schema(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_schema(item) for item in value]
    return value


def parse_json_object(raw_json: str) -> dict[str, Any]:
    """JSON文字列をobjectへ変換し、読めなければproviderの結果の失敗にする。"""
    try:
        payload = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if isinstance(payload, dict):
        return payload
    raise AIProviderResultError(reason=AIProviderResultReason.RESPONSE_UNPARSEABLE)
