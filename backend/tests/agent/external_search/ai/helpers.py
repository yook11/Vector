"""Shared DeepSeek adapter test helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
from openai import APIStatusError

from app.agent.external_search import ExternalSearchCandidate
from app.agent.external_search.ai.spec import DEEPSEEK_QUERY_GENERATOR_SPEC
from app.agent.planning.contract import ExternalResearchTask
from app.shared.security.safe_url import SafeUrl

DEEPSEEK_AS_OF = datetime(2026, 7, 5, tzinfo=UTC)


def make_request() -> httpx.Request:
    return httpx.Request("POST", "https://api.deepseek.com/beta/chat/completions")


def make_response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code, request=make_request())


def make_status_error(status_code: int, message: str = "x") -> APIStatusError:
    return APIStatusError(message, response=make_response(status_code), body=None)


def stub_response(
    *,
    arguments: str | None,
    tool_name: str = DEEPSEEK_QUERY_GENERATOR_SPEC.tool_name,
    no_tool_calls: bool = False,
) -> MagicMock:
    response = MagicMock()
    choice = MagicMock()

    if no_tool_calls:
        choice.message.tool_calls = None
    else:
        tool_call = MagicMock()
        tool_call.function = MagicMock()
        tool_call.function.name = tool_name
        tool_call.function.arguments = arguments or ""
        choice.message.tool_calls = [tool_call]

    response.choices = [choice]
    return response


def patch_adapter_call(
    adapter: object,
    *,
    response: MagicMock | None = None,
    side_effect: Exception | None = None,
) -> AsyncMock:
    mock_call = AsyncMock()
    if side_effect is not None:
        mock_call.side_effect = side_effect
    else:
        mock_call.return_value = response

    client = MagicMock()
    client.chat.completions.create = mock_call
    adapter._client = client
    return mock_call


def task(marker: str = "DeepSeek prompt marker") -> ExternalResearchTask:
    return ExternalResearchTask(collection_goal=f"NVIDIA の最新動向を調査する {marker}")


def candidate(
    marker: str = "CANDIDATE_PROMPT_MARKER",
    *,
    url_marker: str = "SHOULD_NOT_APPEAR",
) -> ExternalSearchCandidate:
    return ExternalSearchCandidate(
        url=SafeUrl(f"https://example.com/news?marker={url_marker}"),
        title=f"NVIDIA product update {marker}",
        snippet=f"NVIDIA announced a supply update {marker}",
        source_name="example.com",
        published_at=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
