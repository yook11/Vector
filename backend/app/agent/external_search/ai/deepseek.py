"""DeepSeek adapters for external search LLM ports."""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from openai import AsyncOpenAI
from pydantic import ValidationError

from app.agent.external_search.ai.prompts import (
    DeepSeekEvidenceSelectorPrompt,
    DeepSeekQueryGeneratorPrompt,
)
from app.agent.external_search.ai.spec import (
    DEEPSEEK_EVIDENCE_SELECTOR_SPEC,
    DEEPSEEK_QUERY_GENERATOR_SPEC,
    ExternalSearchDeepSeekSpec,
)
from app.agent.external_search.contract import (
    EvidenceSelectionResult,
    ExternalEvidenceSelectorError,
    ExternalQueryGenerationError,
    ExternalSearchCandidate,
)
from app.agent.planning.contract import ExternalResearchTask
from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderError,
)
from app.analysis.deepseek_error_translator import (
    DeepSeekStateReason,
    translate_deepseek_error,
)
from app.config import settings


class ExternalDeepSeekResponseDefect(StrEnum):
    """DeepSeek external-search adapter が検知する envelope 契約違反。"""

    NO_TOOL_CALL = "external_search_deepseek_no_tool_call"
    WRONG_TOOL_NAME = "external_search_deepseek_wrong_tool_name"
    ARGUMENTS_NOT_JSON = "external_search_deepseek_arguments_not_json"
    ARGUMENTS_NOT_DICT = "external_search_deepseek_arguments_not_dict"
    ARGUMENTS_SCHEMA_INVALID = "external_search_deepseek_arguments_schema_invalid"


class DeepSeekQueryGenerator:
    """QueryGenerator port の DeepSeek-V4-Flash 実装。"""

    SPEC: Final[ExternalSearchDeepSeekSpec] = DEEPSEEK_QUERY_GENERATOR_SPEC
    TOOL_DESCRIPTION: Final[str] = "外部ニュース検索に使う英語 keyword query を生成する"

    def __init__(self) -> None:
        self._client = _create_client(self.SPEC)

    async def generate(
        self,
        *,
        task: ExternalResearchTask,
        as_of: datetime,
        target_time_window: str | None,
    ) -> list[str]:
        prompt = DeepSeekQueryGeneratorPrompt.render(
            task=task,
            as_of=as_of,
            target_time_window=target_time_window,
        )
        payload = await _call_tool(
            self._client,
            spec=self.SPEC,
            prompt=prompt,
            tool_description=self.TOOL_DESCRIPTION,
            error_type=ExternalQueryGenerationError,
        )

        queries = payload.get("queries")
        if not isinstance(queries, list):
            raise ExternalQueryGenerationError(
                reason=ExternalDeepSeekResponseDefect.ARGUMENTS_SCHEMA_INVALID
            )
        return [query for query in queries if isinstance(query, str)]


class DeepSeekEvidenceSelector:
    """EvidenceSelector port の DeepSeek-V4-Flash 実装。"""

    SPEC: Final[ExternalSearchDeepSeekSpec] = DEEPSEEK_EVIDENCE_SELECTOR_SPEC
    TOOL_DESCRIPTION: Final[str] = "外部ニュース検索候補から回答根拠を選別する"

    def __init__(self) -> None:
        self._client = _create_client(self.SPEC)

    async def select(
        self,
        *,
        task: ExternalResearchTask,
        candidates: list[ExternalSearchCandidate],
        as_of: datetime,
    ) -> EvidenceSelectionResult:
        prompt = DeepSeekEvidenceSelectorPrompt.render(
            task=task,
            candidates=candidates,
            as_of=as_of,
        )
        payload = await _call_tool(
            self._client,
            spec=self.SPEC,
            prompt=prompt,
            tool_description=self.TOOL_DESCRIPTION,
            error_type=ExternalEvidenceSelectorError,
        )

        selections = payload.get("selections")
        missing = payload.get("missing")
        if not isinstance(selections, list) or not isinstance(missing, list):
            raise ExternalEvidenceSelectorError(
                reason=ExternalDeepSeekResponseDefect.ARGUMENTS_SCHEMA_INVALID
            )

        try:
            return EvidenceSelectionResult.from_raw(
                selections=selections,
                missing=missing,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise ExternalEvidenceSelectorError(
                reason=ExternalDeepSeekResponseDefect.ARGUMENTS_SCHEMA_INVALID
            ) from exc


def _create_client(spec: ExternalSearchDeepSeekSpec) -> AsyncOpenAI:
    api_key = settings.deepseek_api_key.get_secret_value()
    if not api_key:
        raise AIProviderConfigurationError(reason=DeepSeekStateReason.NOT_CONFIGURED)
    return AsyncOpenAI(
        api_key=api_key,
        base_url=spec.base_url,
        timeout=spec.timeout_seconds,
    )


async def _call_tool(
    client: Any,
    *,
    spec: ExternalSearchDeepSeekSpec,
    prompt: str,
    tool_description: str,
    error_type: type[ExternalQueryGenerationError]
    | type[ExternalEvidenceSelectorError],
) -> dict[str, Any]:
    try:
        response = await client.chat.completions.create(
            model=spec.model,
            messages=[{"role": "user", "content": prompt}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": spec.tool_name,
                        "strict": True,
                        "description": tool_description,
                        "parameters": dict(spec.response_schema),
                    },
                }
            ],
            **spec.gen_config,
            **spec.structured_output,
        )
    except Exception as exc:
        port_error = _translate_known_provider_error(exc, error_type=error_type)
        if port_error is None:
            raise
        raise port_error from exc

    return _extract_tool_payload(
        response,
        expected_tool_name=spec.tool_name,
        error_type=error_type,
    )


def _extract_tool_payload(
    response: Any,
    *,
    expected_tool_name: str,
    error_type: type[ExternalQueryGenerationError]
    | type[ExternalEvidenceSelectorError],
) -> dict[str, Any]:
    choice = response.choices[0]
    tool_calls = choice.message.tool_calls or []
    if not tool_calls:
        raise error_type(reason=ExternalDeepSeekResponseDefect.NO_TOOL_CALL)
    tool_call = tool_calls[0]
    if tool_call.function.name != expected_tool_name:
        raise error_type(reason=ExternalDeepSeekResponseDefect.WRONG_TOOL_NAME)

    raw_arguments = tool_call.function.arguments or ""
    try:
        payload = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise error_type(
            reason=ExternalDeepSeekResponseDefect.ARGUMENTS_NOT_JSON
        ) from exc

    if not isinstance(payload, dict):
        raise error_type(reason=ExternalDeepSeekResponseDefect.ARGUMENTS_NOT_DICT)
    return payload


def _translate_known_provider_error(
    exc: Exception,
    *,
    error_type: type[ExternalQueryGenerationError]
    | type[ExternalEvidenceSelectorError],
) -> ExternalQueryGenerationError | ExternalEvidenceSelectorError | None:
    translated = translate_deepseek_error(exc)
    if translated is exc:
        return None
    return error_type(reason=_provider_error_reason(translated))


def _provider_error_reason(exc: Exception) -> str | None:
    if isinstance(exc, AIProviderError):
        return getattr(exc, "reason", None)
    return None
