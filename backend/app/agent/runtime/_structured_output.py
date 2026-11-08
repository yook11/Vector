"""Provider-neutralなstructured outputのschema変換。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def thaw_schema(value: Any) -> Any:
    """Agentの凍結schemaをprovider request用のJSON互換値へ戻す。"""
    if isinstance(value, Mapping):
        return {key: thaw_schema(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_schema(item) for item in value]
    return value
