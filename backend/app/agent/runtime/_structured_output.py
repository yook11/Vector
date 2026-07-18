"""Provider-neutralなstructured output parseとvalidation。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, get_args

from pydantic import BaseModel, TypeAdapter, ValidationError

from app.agent.agent import Agent
from app.agent.runtime.contract import (
    AgentResponseDefect,
    AgentResponseInvalidError,
)

_SAFE_CONSTRAINT_KEYS = frozenset(
    {
        "ge",
        "gt",
        "le",
        "lt",
        "max_length",
        "min_length",
        "multiple_of",
    }
)


def thaw_schema(value: Any) -> Any:
    """Agentの凍結schemaをprovider request用のJSON互換値へ戻す。"""
    if isinstance(value, Mapping):
        return {key: thaw_schema(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_schema(item) for item in value]
    return value


def parse_json_object(raw_json: str) -> dict[str, Any]:
    """JSON文字列をobjectへ変換し、共通defectへ分類する。"""
    try:
        payload = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        defect = AgentResponseDefect.RESPONSE_NOT_JSON
        repair_hint = "response must be valid JSON"
    else:
        if isinstance(payload, dict):
            return payload
        defect = AgentResponseDefect.RESPONSE_NOT_OBJECT
        repair_hint = "response root must be a JSON object"
    raise AgentResponseInvalidError(defect, repair_hint=repair_hint)


def validate_output[InputT, OutputT](
    agent: Agent[InputT, OutputT],
    payload: dict[str, Any],
) -> OutputT:
    """JSON objectをAgentが宣言したPython outputへ検証する。"""
    try:
        return TypeAdapter(agent.output_type).validate_python(payload)
    except ValidationError as exc:
        repair_hint = _validation_repair_hint(
            exc,
            allowed_locations=_declared_location_names(agent.output_type),
        )
    raise AgentResponseInvalidError(
        AgentResponseDefect.OUTPUT_SCHEMA_MISMATCH,
        repair_hint=repair_hint,
    )


def _validation_repair_hint(
    error: ValidationError,
    *,
    allowed_locations: frozenset[str],
) -> str:
    repairs: list[str] = []
    for detail in error.errors(include_input=False):
        field_path = _field_path(
            detail.get("loc"),
            allowed_locations=allowed_locations,
        )
        parts = [f"field={field_path}"]
        error_type = detail.get("type")
        if isinstance(error_type, str):
            parts.append(f"type={error_type}")
        context = detail.get("ctx")
        if isinstance(context, Mapping):
            for key in sorted(_SAFE_CONSTRAINT_KEYS & context.keys()):
                value = context[key]
                if value is None or type(value) in {str, int, float, bool}:
                    parts.append(f"{key}={value}")
        repairs.append(" ".join(parts))
    return "; ".join(repairs) or "output does not match the declared schema"


def _field_path(
    location: object,
    *,
    allowed_locations: frozenset[str],
) -> str:
    if not isinstance(location, (list, tuple)):
        return "root"
    components = [
        str(part)
        if type(part) is int or isinstance(part, str) and part in allowed_locations
        else "[unknown]"
        for part in location
    ]
    return ".".join(components) or "root"


def _declared_location_names(output_type: type[Any]) -> frozenset[str]:
    locations: set[str] = set()
    pending: list[type[BaseModel]] = []
    visited: set[type[BaseModel]] = set()
    _append_model_type(output_type, pending)

    while pending:
        model_type = pending.pop()
        if model_type in visited:
            continue
        visited.add(model_type)
        for field_name, field in model_type.model_fields.items():
            locations.add(field_name)
            for alias in (
                field.alias,
                field.validation_alias,
                field.serialization_alias,
            ):
                if isinstance(alias, str):
                    locations.add(alias)
            _append_annotation_models(field.annotation, pending)
    return frozenset(locations)


def _append_annotation_models(
    annotation: object,
    pending: list[type[BaseModel]],
) -> None:
    _append_model_type(annotation, pending)
    for argument in get_args(annotation):
        _append_annotation_models(argument, pending)


def _append_model_type(
    candidate: object,
    pending: list[type[BaseModel]],
) -> None:
    if isinstance(candidate, type) and issubclass(candidate, BaseModel):
        pending.append(candidate)
