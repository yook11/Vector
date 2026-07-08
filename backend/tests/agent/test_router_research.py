"""Research response API router contract tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

import app.agent.router as research_router_module
from app.agent.answering.direct import DirectAnswerInvalidError
from app.agent.contract import (
    AnswerQuestionInput,
    AnswerQuestionResult,
    AnswerRetrievalSummary,
    ExternalUrlSource,
    InternalArticleSource,
)
from app.agent.router import get_question_answering_agent
from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderError,
)
from app.config import settings
from app.main import app
from app.shared.security.safe_url import SafeUrl

_URL = "/api/v1/research/responses"


class FakeQuestionAnsweringAgent:
    def __init__(
        self,
        result: AnswerQuestionResult | None = None,
        exc: Exception | None = None,
    ) -> None:
        self._result = result
        self._exc = exc
        self.calls: list[AnswerQuestionInput] = []

    async def answer(self, input: AnswerQuestionInput) -> AnswerQuestionResult:
        self.calls.append(input)
        if self._exc is not None:
            raise self._exc
        if self._result is None:
            raise AssertionError("fake result is not configured")
        return self._result


def _retrieval(
    planned_mode: str = "internal",
) -> AnswerRetrievalSummary:
    return AnswerRetrievalSummary(
        planned_mode=planned_mode,  # type: ignore[arg-type]
        unmet_requirements=[],
    )


def _answered_with_sources() -> AnswerQuestionResult:
    return AnswerQuestionResult(
        status="answered",
        answer="NVIDIA は新製品発表後も需要が強いです。[[1]][[2]]",
        sources=[
            InternalArticleSource(
                source_ref="1",
                article_id=123,
                title="GPU 需要の分析",
                snippet="データセンター向け GPU 需要が強い。",
                published_at=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
                source_name="Vector",
            ),
            ExternalUrlSource(
                source_ref="2",
                url=SafeUrl("https://example.com/nvidia-demand"),
                title="NVIDIA demand update",
                snippet="Analysts point to continued demand.",
                published_at=datetime(2026, 7, 2, 12, 0, tzinfo=UTC),
                source_name="Example News",
            ),
        ],
        missing_aspects=[],
        retrieval=_retrieval("internal_and_external"),
    )


def _direct_answer() -> AnswerQuestionResult:
    return AnswerQuestionResult(
        status="answered",
        answer="検索なしの一般回答です。",
        sources=[],
        missing_aspects=[],
        retrieval=_retrieval("none"),
    )


def _insufficient_answer() -> AnswerQuestionResult:
    return AnswerQuestionResult(
        status="insufficient",
        answer="確認できた範囲では需要は強いですが、一部指標は未確認です。[[1]]",
        sources=[
            InternalArticleSource(
                source_ref="1",
                article_id=123,
                title="GPU 需要の分析",
            )
        ],
        missing_aspects=["直近四半期の出荷数は確認できませんでした"],
        retrieval=_retrieval("internal"),
    )


def _metadata_null_answer() -> AnswerQuestionResult:
    return AnswerQuestionResult(
        status="answered",
        answer="metadata が欠けた source の回答です。[[1]]",
        sources=[
            InternalArticleSource(
                source_ref="1",
                article_id=123,
                title="metadata 欠損記事",
            )
        ],
        missing_aspects=[],
        retrieval=_retrieval("internal"),
    )


def _override_agent(agent: FakeQuestionAnsweringAgent) -> None:
    app.dependency_overrides[get_question_answering_agent] = lambda: agent


@pytest.fixture
async def research_client(
    auth_headers: dict[str, str],
) -> AsyncGenerator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        yield client
    app.dependency_overrides.pop(get_question_answering_agent, None)


@pytest.fixture
async def anonymous_research_client() -> AsyncGenerator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client
    app.dependency_overrides.pop(get_question_answering_agent, None)


@pytest.fixture
async def research_client_no_raise(
    auth_headers: dict[str, str],
) -> AsyncGenerator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers=auth_headers,
    ) as client:
        yield client
    app.dependency_overrides.pop(get_question_answering_agent, None)


@pytest.mark.asyncio
class TestCreateResearchResponse:
    async def test_maps_result_to_public_contract(
        self,
        research_client: AsyncClient,
    ) -> None:
        _override_agent(FakeQuestionAnsweringAgent(_answered_with_sources()))

        response = await research_client.post(_URL, json={"question": "NVIDIA は？"})

        assert response.status_code == 200
        data = response.json()
        assert set(data) == {"answer", "sources", "missingAspects"}
        assert data["answer"] == "NVIDIA は新製品発表後も需要が強いです。[[1]][[2]]"
        assert data["missingAspects"] == []
        assert data["sources"] == [
            {
                "kind": "internal_article",
                "sourceRef": "1",
                "articleId": 123,
                "title": "GPU 需要の分析",
                "snippet": "データセンター向け GPU 需要が強い。",
                "publishedAt": "2026-07-01T09:00:00Z",
                "sourceName": "Vector",
            },
            {
                "kind": "external_url",
                "sourceRef": "2",
                "url": "https://example.com/nvidia-demand",
                "title": "NVIDIA demand update",
                "snippet": "Analysts point to continued demand.",
                "publishedAt": "2026-07-02T12:00:00Z",
                "sourceName": "Example News",
            },
        ]
        assert "status" not in data
        assert "retrieval" not in data
        assert "sufficiency" not in data

    async def test_source_nullable_metadata_is_present_as_null(
        self,
        research_client: AsyncClient,
    ) -> None:
        _override_agent(FakeQuestionAnsweringAgent(_metadata_null_answer()))

        response = await research_client.post(_URL, json={"question": "metadata は？"})

        assert response.status_code == 200
        source = response.json()["sources"][0]
        assert "sourceName" in source and source["sourceName"] is None
        assert "publishedAt" in source and source["publishedAt"] is None
        assert "snippet" in source and source["snippet"] is None

    async def test_direct_answer_keeps_empty_sources_and_missing_aspects(
        self,
        research_client: AsyncClient,
    ) -> None:
        _override_agent(FakeQuestionAnsweringAgent(_direct_answer()))

        response = await research_client.post(_URL, json={"question": "用語説明して"})

        assert response.status_code == 200
        assert response.json() == {
            "answer": "検索なしの一般回答です。",
            "sources": [],
            "missingAspects": [],
        }

    async def test_insufficient_answer_exposes_missing_aspects_without_status(
        self,
        research_client: AsyncClient,
    ) -> None:
        _override_agent(FakeQuestionAnsweringAgent(_insufficient_answer()))

        response = await research_client.post(_URL, json={"question": "需要は？"})

        assert response.status_code == 200
        data = response.json()
        assert data["missingAspects"] == ["直近四半期の出荷数は確認できませんでした"]
        assert "status" not in data
        assert "retrieval" not in data

    async def test_passes_stripped_question_and_utc_aware_as_of(
        self,
        research_client: AsyncClient,
    ) -> None:
        agent = FakeQuestionAnsweringAgent(_direct_answer())
        _override_agent(agent)

        response = await research_client.post(_URL, json={"question": "  用語説明  "})

        assert response.status_code == 200
        assert len(agent.calls) == 1
        input_ = agent.calls[0]
        assert input_.question == "用語説明"
        assert input_.as_of.tzinfo is UTC

    async def test_requires_auth(
        self,
        anonymous_research_client: AsyncClient,
    ) -> None:
        _override_agent(FakeQuestionAnsweringAgent(_direct_answer()))

        response = await anonymous_research_client.post(
            _URL, json={"question": "NVIDIA は？"}
        )

        assert response.status_code == 401
        assert response.json() == {"detail": "Not authenticated"}

    @pytest.mark.parametrize(
        "question",
        ["", "   ", "あ" * 1001],
    )
    async def test_rejects_invalid_question(
        self,
        research_client: AsyncClient,
        question: str,
    ) -> None:
        _override_agent(FakeQuestionAnsweringAgent(_direct_answer()))

        response = await research_client.post(_URL, json={"question": question})

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert isinstance(detail, list)
        assert detail[0]["loc"] == ["body", "question"]

    async def test_accepts_question_at_max_length(
        self,
        research_client: AsyncClient,
    ) -> None:
        _override_agent(FakeQuestionAnsweringAgent(_direct_answer()))

        response = await research_client.post(_URL, json={"question": "あ" * 1000})

        assert response.status_code == 200

    @pytest.mark.parametrize(
        "exc",
        [
            AIProviderError("provider internal reason SHOULD_NOT_LEAK"),
            DirectAnswerInvalidError("direct_internal_reason_SHOULD_NOT_LEAK"),
        ],
    )
    async def test_typed_generation_errors_return_generic_503(
        self,
        research_client: AsyncClient,
        exc: Exception,
    ) -> None:
        _override_agent(FakeQuestionAnsweringAgent(exc=exc))

        response = await research_client.post(_URL, json={"question": "NVIDIA は？"})

        assert response.status_code == 503
        assert response.json() == {
            "detail": "Answer generation is temporarily unavailable"
        }
        assert "SHOULD_NOT_LEAK" not in response.text

    async def test_unexpected_error_stays_500(
        self,
        research_client_no_raise: AsyncClient,
    ) -> None:
        _override_agent(FakeQuestionAnsweringAgent(exc=RuntimeError("boom")))

        response = await research_client_no_raise.post(
            _URL, json={"question": "NVIDIA は？"}
        )

        assert response.status_code == 500


def _resolve_ref(schema: dict[str, Any], ref: str) -> dict[str, Any]:
    name = ref.removeprefix("#/components/schemas/")
    return schema["components"]["schemas"][name]


def test_openapi_exposes_operation_id_and_question_max_length() -> None:
    app.openapi_schema = None
    schema = app.openapi()
    operation = schema["paths"][_URL]["post"]
    body_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    request_schema = _resolve_ref(schema, body_schema["$ref"])
    response_body_schema = operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    response_schema = _resolve_ref(schema, response_body_schema["$ref"])

    assert operation["operationId"] == "create_research_response"
    assert request_schema["properties"]["question"]["maxLength"] == 1000
    assert (
        "citation markers like [[1]]"
        in response_schema["properties"]["answer"]["description"]
    )


@pytest.mark.parametrize(
    ("deepseek_key", "tavily_key"),
    [
        ("", "tvly-test-key"),
        ("deepseek-test-key", ""),
        ("", ""),
    ],
)
def test_external_search_key_missing_is_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
    deepseek_key: str,
    tavily_key: str,
) -> None:
    monkeypatch.setattr(settings, "deepseek_api_key", SecretStr(deepseek_key))
    monkeypatch.setattr(settings, "tavily_api_key", SecretStr(tavily_key))

    with pytest.raises(AIProviderConfigurationError):
        research_router_module._build_external_search(object())  # type: ignore[arg-type]


def test_agent_factory_maps_configuration_error_to_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_configuration_error(**_kwargs: object) -> None:
        raise AIProviderConfigurationError()

    monkeypatch.setattr(
        research_router_module,
        "_build_question_answering_agent",
        raise_configuration_error,
    )

    with pytest.raises(HTTPException) as exc_info:
        get_question_answering_agent(
            session=object(),  # type: ignore[arg-type]
            tavily_client=object(),  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Answer generation is temporarily unavailable"
