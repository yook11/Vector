"""Agent composition public builder behavior tests。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import SecretStr

from app.agent import composition
from app.agent.planning.agent import QUESTION_PLANNER_AGENT
from app.agent.question_context.agent import QUESTION_CONTEXT_AGENT
from app.agent.question_context.contract import (
    QuestionContextDraft,
    QuestionContextPreparationResult,
)
from app.agent.question_context.service import QuestionContextService
from app.agent.running import AnsweringPhases, AnsweringRunner
from app.agent.runtime.contract import (
    AgentResponseDefect,
    AgentResponseInvalidError,
)
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.analysis.ai_provider_errors import AIProviderError


@dataclass(frozen=True, slots=True)
class _FakeTavilyClient:
    invocation: int


class _TrackedClientContext:
    def __init__(
        self,
        *,
        client: _FakeTavilyClient,
        lifecycle: list[str],
    ) -> None:
        self._client = client
        self._lifecycle = lifecycle

    async def __aenter__(self) -> _FakeTavilyClient:
        self._lifecycle.append(f"client {self._client.invocation} enter")
        return self._client

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> bool:
        self._lifecycle.append(f"client {self._client.invocation} exit")
        return False


class _SafeClientFactory:
    def __init__(self, lifecycle: list[str]) -> None:
        self._lifecycle = lifecycle
        self.clients: list[_FakeTavilyClient] = []

    def __call__(self) -> _TrackedClientContext:
        client = _FakeTavilyClient(invocation=len(self.clients) + 1)
        self.clients.append(client)
        return _TrackedClientContext(client=client, lifecycle=self._lifecycle)


class _FakeDeepSeekClient:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.aclose = AsyncMock()
        self.close = AsyncMock()


class _FakeDeepSeekClientFactory:
    def __init__(self) -> None:
        self.clients: list[_FakeDeepSeekClient] = []

    def __call__(self, **kwargs: object) -> _FakeDeepSeekClient:
        client = _FakeDeepSeekClient(**kwargs)
        self.clients.append(client)
        return client


def _composition_builder(name: str) -> Any:
    builder = getattr(composition, name, None)
    if builder is None:
        pytest.fail(
            f"app.agent.composition.{name} が未実装です",
            pytrace=False,
        )
    return builder


def test_build_answering_runner_wires_question_context_agent_and_deferred_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_answering_runner = _composition_builder("build_answering_runner")
    activation_calls: list[None] = []

    def activate_runtime() -> None:
        activation_calls.append(None)
        raise AssertionError("runner construction must not activate the runtime")

    monkeypatch.setattr(
        composition.settings,
        "gemini_api_key",
        SecretStr("question-context-gemini-key-sentinel"),
    )
    monkeypatch.setattr(
        composition,
        "activate_gemini_agent_runtime",
        activate_runtime,
    )

    runner = build_answering_runner(session_factory=object())
    context_preparer = runner._context_preparer

    assert isinstance(runner, AnsweringRunner)
    assert isinstance(context_preparer, QuestionContextService)
    assert context_preparer._agent is QUESTION_CONTEXT_AGENT
    assert context_preparer._runtime_scope_factory is activate_runtime
    assert activation_calls == []


def test_build_answering_runner_injects_no_runtime_when_gemini_is_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_answering_runner = _composition_builder("build_answering_runner")
    monkeypatch.setattr(
        composition.settings,
        "gemini_api_key",
        SecretStr(""),
    )

    runner = build_answering_runner(session_factory=object())
    context_preparer = runner._context_preparer

    assert isinstance(context_preparer, QuestionContextService)
    assert context_preparer._agent is QUESTION_CONTEXT_AGENT
    assert context_preparer._runtime_scope_factory is None


def test_external_search_service_builder_does_not_activate_external_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_external_search_service = _composition_builder(
        "build_external_search_service"
    )
    activation_calls: list[None] = []

    class _Factory:
        def activate(self) -> None:
            activation_calls.append(None)
            raise AssertionError("service builder must not activate the runtime")

    factory = _Factory()
    monkeypatch.setattr(
        composition,
        "build_external_research_runtime_factory",
        lambda: factory,
    )
    monkeypatch.setattr(
        composition.settings,
        "deepseek_api_key",
        SecretStr("deepseek-api-key-sentinel"),
    )
    monkeypatch.setattr(
        composition.settings,
        "tavily_api_key",
        SecretStr("tavily-api-key-sentinel"),
    )

    service = build_external_search_service()

    assert (service._runtime_factory is factory, activation_calls) == (True, [])


@pytest.mark.parametrize(
    "unexpected_argument", ["tavily_client", "http_client_factory"]
)
def test_answering_runner_builder_rejects_public_client_injection(
    unexpected_argument: str,
) -> None:
    with pytest.raises(TypeError):
        composition.build_answering_runner(
            session_factory=object(),
            **{unexpected_argument: object()},
        )


class _FakeGeminiAsyncClient:
    def __init__(self, invocation: int) -> None:
        self.invocation = invocation


class _FakeGeminiAsyncClientContext:
    def __init__(
        self,
        *,
        client: _FakeGeminiAsyncClient,
        lifecycle: list[str],
    ) -> None:
        self._client = client
        self._lifecycle = lifecycle

    async def __aenter__(self) -> _FakeGeminiAsyncClient:
        self._lifecycle.append(f"gemini {self._client.invocation} enter")
        return self._client

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self._lifecycle.append(f"gemini {self._client.invocation} exit")
        return False


class _FakeGeminiSdkClient:
    def __init__(
        self,
        *,
        client: _FakeGeminiAsyncClient,
        lifecycle: list[str],
    ) -> None:
        self.aio = _FakeGeminiAsyncClientContext(
            client=client,
            lifecycle=lifecycle,
        )


class _FakeGeminiSdkClientFactory:
    def __init__(self, lifecycle: list[str]) -> None:
        self._lifecycle = lifecycle
        self.calls: list[dict[str, object]] = []
        self.async_clients: list[_FakeGeminiAsyncClient] = []

    def __call__(self, **kwargs: object) -> _FakeGeminiSdkClient:
        self.calls.append(kwargs)
        client = _FakeGeminiAsyncClient(len(self.async_clients) + 1)
        self.async_clients.append(client)
        self._lifecycle.append(f"gemini {client.invocation} create")
        return _FakeGeminiSdkClient(client=client, lifecycle=self._lifecycle)


class _FakeGeminiRuntime:
    constructed: list[_FakeGeminiRuntime] = []
    construction_error: BaseException | None = None
    outcome: QuestionContextDraft | BaseException | None = None
    calls: list[tuple[object, object, int]] = []

    def __init__(self, *, client: _FakeGeminiAsyncClient) -> None:
        if self.construction_error is not None:
            raise self.construction_error
        self.client = client
        self.constructed.append(self)

    async def invoke(
        self,
        agent: object,
        input: object,
        *,
        attempt_number: int,
    ) -> QuestionContextDraft:
        self.calls.append((agent, input, attempt_number))
        outcome = self.outcome
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is None:
            raise AssertionError("fake runtime outcome is not configured")
        return outcome


def _install_gemini_runtime_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    lifecycle: list[str],
    construction_error: BaseException | None = None,
) -> _FakeGeminiSdkClientFactory:
    from google import genai as genai_module

    from app.agent.runtime import gemini as runtime_gemini

    client_factory = _FakeGeminiSdkClientFactory(lifecycle)
    _FakeGeminiRuntime.constructed = []
    _FakeGeminiRuntime.construction_error = construction_error
    _FakeGeminiRuntime.outcome = None
    _FakeGeminiRuntime.calls = []
    monkeypatch.setattr(genai_module, "Client", client_factory)
    monkeypatch.setattr(runtime_gemini, "GeminiAgentRuntime", _FakeGeminiRuntime)
    monkeypatch.setattr(
        composition.settings,
        "gemini_api_key",
        SecretStr("gemini-api-key-sentinel"),
    )
    return client_factory


async def test_gemini_agent_runtime_scope_is_lazy_and_uses_sdk_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activate_gemini_agent_runtime = _composition_builder(
        "activate_gemini_agent_runtime"
    )
    lifecycle: list[str] = []
    client_factory = _install_gemini_runtime_fakes(
        monkeypatch,
        lifecycle=lifecycle,
    )

    scope = activate_gemini_agent_runtime()
    assert (client_factory.calls, lifecycle, _FakeGeminiRuntime.constructed) == (
        [],
        [],
        [],
    )

    async with scope as runtime:
        assert runtime is _FakeGeminiRuntime.constructed[0]
        assert runtime.client is client_factory.async_clients[0]
        assert lifecycle == ["gemini 1 create", "gemini 1 enter"]

    assert lifecycle == ["gemini 1 create", "gemini 1 enter", "gemini 1 exit"]
    assert client_factory.calls == [{"api_key": "gemini-api-key-sentinel"}]


@pytest.mark.parametrize(
    "body_error",
    [
        pytest.param(AIProviderError(), id="provider-error"),
        pytest.param(
            AgentResponseInvalidError(AgentResponseDefect.RESPONSE_NOT_JSON),
            id="response-error",
        ),
        pytest.param(RuntimeError("runtime scope body failed"), id="body-error"),
        pytest.param(asyncio.CancelledError(), id="cancellation"),
    ],
)
async def test_gemini_agent_runtime_scope_closes_once_on_abnormal_body_exit(
    monkeypatch: pytest.MonkeyPatch,
    body_error: BaseException,
) -> None:
    activate_gemini_agent_runtime = _composition_builder(
        "activate_gemini_agent_runtime"
    )
    lifecycle: list[str] = []
    _install_gemini_runtime_fakes(monkeypatch, lifecycle=lifecycle)

    with pytest.raises(type(body_error)) as raised:
        async with activate_gemini_agent_runtime():
            raise body_error

    assert raised.value is body_error
    assert lifecycle == ["gemini 1 create", "gemini 1 enter", "gemini 1 exit"]


async def test_gemini_agent_runtime_scope_closes_when_runtime_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activate_gemini_agent_runtime = _composition_builder(
        "activate_gemini_agent_runtime"
    )
    lifecycle: list[str] = []
    error = RuntimeError("runtime construction failed")
    _install_gemini_runtime_fakes(
        monkeypatch,
        lifecycle=lifecycle,
        construction_error=error,
    )

    with pytest.raises(RuntimeError) as raised:
        async with activate_gemini_agent_runtime():
            raise AssertionError("scope body must not start")

    assert raised.value is error
    assert lifecycle == ["gemini 1 create", "gemini 1 enter", "gemini 1 exit"]


async def test_gemini_agent_runtime_scope_creates_fresh_resources_each_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activate_gemini_agent_runtime = _composition_builder(
        "activate_gemini_agent_runtime"
    )
    lifecycle: list[str] = []
    client_factory = _install_gemini_runtime_fakes(
        monkeypatch,
        lifecycle=lifecycle,
    )

    async with activate_gemini_agent_runtime() as first_runtime:
        pass
    async with activate_gemini_agent_runtime() as second_runtime:
        pass

    assert len(client_factory.async_clients) == 2
    assert client_factory.async_clients[0] is not client_factory.async_clients[1]
    assert first_runtime is not second_runtime
    assert first_runtime.client is client_factory.async_clients[0]
    assert second_runtime.client is client_factory.async_clients[1]
    assert lifecycle == [
        "gemini 1 create",
        "gemini 1 enter",
        "gemini 1 exit",
        "gemini 2 create",
        "gemini 2 enter",
        "gemini 2 exit",
    ]


@pytest.mark.parametrize(
    ("outcome", "expected_question", "propagates"),
    [
        pytest.param(
            QuestionContextDraft(standalone_question="prepared question"),
            "prepared question",
            False,
            id="success",
        ),
        pytest.param(
            AgentResponseInvalidError(AgentResponseDefect.RESPONSE_NOT_JSON),
            "original question",
            False,
            id="classified-failure",
        ),
        pytest.param(
            QuestionContextDraft(standalone_question="   "),
            "original question",
            False,
            id="finalize-failure",
        ),
        pytest.param(
            RuntimeError("unexpected runtime failure"),
            None,
            True,
            id="unknown-failure",
        ),
        pytest.param(
            asyncio.CancelledError(),
            None,
            True,
            id="cancellation",
        ),
    ],
)
async def test_question_context_service_closes_production_gemini_scope_once(
    monkeypatch: pytest.MonkeyPatch,
    outcome: QuestionContextDraft | BaseException,
    expected_question: str | None,
    propagates: bool,
) -> None:
    lifecycle: list[str] = []
    _install_gemini_runtime_fakes(monkeypatch, lifecycle=lifecycle)
    _FakeGeminiRuntime.outcome = outcome
    service = QuestionContextService(
        agent=QUESTION_CONTEXT_AGENT,
        runtime_scope_factory=composition.activate_gemini_agent_runtime,
    )

    async def prepare() -> QuestionContextPreparationResult:
        return await service.prepare(
            question="original question",
            history=[ThreadMessageSnapshot(role="user", content="prior question")],
            as_of=datetime(2026, 7, 19, tzinfo=UTC),
            run_id=UUID("00000000-0000-4000-a000-000000000020"),
        )

    if propagates:
        with pytest.raises(type(outcome)) as raised:
            await prepare()
        assert raised.value is outcome
    else:
        result = await prepare()
        assert result.context.standalone_question == expected_question

    assert len(_FakeGeminiRuntime.calls) == 1
    assert _FakeGeminiRuntime.calls[0][0] is QUESTION_CONTEXT_AGENT
    assert _FakeGeminiRuntime.calls[0][2] == 1
    assert lifecycle == ["gemini 1 create", "gemini 1 enter", "gemini 1 exit"]


class _KeywordObject:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs


def test_build_answering_phases_wires_planner_to_shared_gemini_runtime_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.agent.answering.direct_answer import flow as direct_flow
    from app.agent.answering.direct_answer.agent import DIRECT_ANSWER_AGENT
    from app.agent.answering.evidence_answer import flow as evidence_flow
    from app.agent.answering.evidence_answer.agent import EVIDENCE_ANSWER_AGENT
    from app.agent.evidence_collection import service as evidence_service
    from app.agent.evidence_collection.internal_search import (
        article_search,
    )
    from app.agent.evidence_collection.internal_search import (
        service as internal_service,
    )
    from app.agent.evidence_collection.internal_search.ai import (
        gemini as embedding_gemini,
    )
    from app.agent.planning import service as planning_service

    planner_calls: list[dict[str, object]] = []
    direct_calls: list[dict[str, object]] = []
    evidence_calls: list[dict[str, object]] = []

    class _PlannerSpy(_KeywordObject):
        def __init__(self, **kwargs: object) -> None:
            planner_calls.append(kwargs)
            super().__init__(**kwargs)

    class _DirectSpy(_KeywordObject):
        def __init__(self, **kwargs: object) -> None:
            direct_calls.append(kwargs)
            super().__init__(**kwargs)

    class _EvidenceSpy(_KeywordObject):
        def __init__(self, **kwargs: object) -> None:
            evidence_calls.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(
        composition,
        "ensure_external_search_configured",
        lambda: None,
    )
    monkeypatch.setattr(
        composition, "build_external_search_service", lambda *_a, **_k: object()
    )
    monkeypatch.setattr(planning_service, "QuestionPlanningService", _PlannerSpy)
    monkeypatch.setattr(direct_flow, "DirectAnswerFlow", _DirectSpy)
    monkeypatch.setattr(evidence_flow, "EvidenceAnswerFlow", _EvidenceSpy)
    for module, name in (
        (evidence_service, "EvidenceCollectionService"),
        (embedding_gemini, "GeminiQueryEmbedder"),
        (article_search, "PgVectorArticleSearchRepository"),
        (internal_service, "InternalSearchService"),
    ):
        monkeypatch.setattr(module, name, _KeywordObject)

    phases = composition._build_answering_phases(
        session_factory=object(),
    )

    assert isinstance(phases, AnsweringPhases)
    assert planner_calls == [
        {
            "agent": QUESTION_PLANNER_AGENT,
            "runtime_scope_factory": _composition_builder(
                "activate_gemini_agent_runtime"
            ),
        }
    ]
    assert direct_calls == [
        {
            "agent": DIRECT_ANSWER_AGENT,
            "runtime_scope_factory": _composition_builder(
                "activate_gemini_agent_runtime"
            ),
            "delta_reporter": None,
            "continuation": None,
        }
    ]
    assert evidence_calls == [
        {
            "agent": EVIDENCE_ANSWER_AGENT,
            "runtime_scope_factory": _composition_builder(
                "activate_gemini_agent_runtime"
            ),
            "delta_reporter": None,
            "continuation": None,
        }
    ]


def test_build_answering_runner_captures_phase_dependencies_without_building_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []
    phase_bundle = object()
    session_factory = object()
    progress = object()
    events = object()
    delta_reporter = object()
    continuation = object()

    monkeypatch.setattr(
        composition,
        "_build_answering_phases",
        lambda **kwargs: captured.append(kwargs) or phase_bundle,
        raising=False,
    )

    runner = composition.build_answering_runner(
        session_factory=session_factory,
        progress=progress,
        events=events,
        delta_reporter=delta_reporter,
        continuation=continuation,
    )

    assert captured == []
    assert runner._phases_factory() is phase_bundle
    assert captured == [
        {
            "session_factory": session_factory,
            "events": events,
            "delta_reporter": delta_reporter,
            "continuation": continuation,
        }
    ]
