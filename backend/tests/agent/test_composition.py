"""Agent composition public builder behavior tests。"""

from __future__ import annotations

import asyncio
from types import TracebackType
from typing import cast

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import composition
from app.agent.composition import (
    activate_gemini_agent_runtime,
)
from app.agent.planning.agent import QUESTION_PLANNER_AGENT
from app.agent.running import AnsweringPhases
from app.agent.runtime.contract import (
    AgentResponseDefect,
    AgentResponseInvalidError,
)
from app.analysis.ai_provider_errors import (
    AIProviderError,
)


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
    outcome: object | BaseException | None = None
    calls: list[tuple[object, object, int]] = []

    def __init__(self, *, client: _FakeGeminiAsyncClient) -> None:
        if self.construction_error is not None:
            raise self.construction_error
        self.client = client
        self.constructed.append(self)

    async def call(
        self,
        agent: object,
        input: object,
        *,
        attempt_number: int,
    ) -> object:
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


class _KeywordObject:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs


def test_build_answering_phases_wires_planner_to_shared_gemini_runtime_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.agent.answering.direct_answer import service as direct_service
    from app.agent.answering.direct_answer.agent import DIRECT_ANSWER_AGENT
    from app.agent.answering.evidence_answer import service as evidence_service
    from app.agent.answering.evidence_answer.agent import EVIDENCE_ANSWER_AGENT
    from app.agent.evidence_collection.internal_search import (
        article_repository,
    )
    from app.agent.evidence_collection.internal_search import (
        service as internal_search_service,
    )
    from app.agent.evidence_collection.internal_search.ai import (
        gemini as embedding_gemini,
    )
    from app.agent.planning import service as planning_service

    planner_calls: list[dict[str, object]] = []
    direct_calls: list[dict[str, object]] = []
    evidence_calls: list[dict[str, object]] = []
    internal_search = object()
    events = object()

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
    monkeypatch.setattr(planning_service, "QuestionPlanningService", _PlannerSpy)
    monkeypatch.setattr(direct_service, "DirectAnswerService", _DirectSpy)
    monkeypatch.setattr(evidence_service, "EvidenceAnswerService", _EvidenceSpy)
    for module, name in (
        (embedding_gemini, "GeminiQueryEmbedder"),
        (article_repository, "PgVectorArticleSearchRepository"),
    ):
        monkeypatch.setattr(module, name, _KeywordObject)
    internal_search_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        internal_search_service,
        "InternalSearchService",
        lambda **kwargs: internal_search_calls.append(kwargs) or internal_search,
    )

    phases = composition._build_answering_phases(
        session_factory=object(),
        events=events,
    )

    assert isinstance(phases, AnsweringPhases)
    # events(段2でserviceに渡さないとした進捗reporter)は収集サービスへ渡る。
    assert phases.collector.internal_search is internal_search
    assert phases.collector.events is events
    assert set(internal_search_calls[0]) == {
        "embedder",
        "article_search_repository",
        "query_embedding_cache",
    }
    assert planner_calls == [
        {
            "agent": QUESTION_PLANNER_AGENT,
            "runtime_scope_factory": activate_gemini_agent_runtime,
        }
    ]
    assert direct_calls == [
        {
            "agent": DIRECT_ANSWER_AGENT,
            "runtime_scope_factory": activate_gemini_agent_runtime,
            "delta_reporter": None,
            "continuation": None,
        }
    ]
    assert evidence_calls == [
        {
            "agent": EVIDENCE_ANSWER_AGENT,
            "runtime_scope_factory": activate_gemini_agent_runtime,
            "delta_reporter": None,
            "continuation": None,
        }
    ]


def test_build_answering_phases_wires_query_embedding_cache_to_embedder_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.agent.evidence_collection.internal_search import (
        article_repository,
    )
    from app.agent.evidence_collection.internal_search import (
        service as internal_search_service,
    )
    from app.agent.evidence_collection.internal_search.ai import (
        gemini as embedding_gemini,
    )
    from app.agent.evidence_collection.internal_search.ai.gemini_spec import (
        GEMINI_QUERY_EMBEDDING_SPEC,
        embedder_identity_of,
    )

    session_factory = object()
    internal_search_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        composition,
        "ensure_external_search_configured",
        lambda: None,
    )
    for module, name in (
        (embedding_gemini, "GeminiQueryEmbedder"),
        (article_repository, "PgVectorArticleSearchRepository"),
    ):
        monkeypatch.setattr(module, name, _KeywordObject)
    monkeypatch.setattr(
        internal_search_service,
        "InternalSearchService",
        lambda **kwargs: internal_search_calls.append(kwargs) or object(),
    )

    composition._build_answering_phases(session_factory=session_factory)

    cache = internal_search_calls[0]["query_embedding_cache"]
    # embedderが実際に使うspecとキャッシュ空間が一致しないと、別条件で作った
    # ベクトルをstale hitとして再利用してしまう。
    assert cache.embedder_identity == embedder_identity_of(GEMINI_QUERY_EMBEDDING_SPEC)
    assert cache.session_factory is session_factory


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


def test_composition_injects_same_live_controls_into_both_answer_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.agent.answering.direct_answer.service as direct_service_module
    import app.agent.answering.evidence_answer.service as evidence_service_module
    import app.agent.evidence_collection.internal_search.ai.gemini as embedder_module
    import app.agent.planning.service as planning_service_module
    from app.agent.answering.direct_answer.agent import DIRECT_ANSWER_AGENT
    from app.agent.answering.evidence_answer.agent import EVIDENCE_ANSWER_AGENT
    from app.agent.evidence_collection.internal_search import (
        article_repository as article_repository_module,
    )
    from app.agent.evidence_collection.internal_search import (
        service as internal_search_module,
    )

    captured: dict[str, dict[str, object]] = {}
    internal_search = object()

    def capture_direct(**kwargs: object) -> object:
        captured["direct"] = kwargs
        return object()

    def capture_evidence(**kwargs: object) -> object:
        captured["evidence"] = kwargs
        return object()

    monkeypatch.setattr(
        composition,
        "ensure_external_search_configured",
        lambda: None,
    )
    monkeypatch.setattr(direct_service_module, "DirectAnswerService", capture_direct)
    monkeypatch.setattr(
        evidence_service_module, "EvidenceAnswerService", capture_evidence
    )
    monkeypatch.setattr(embedder_module, "GeminiQueryEmbedder", lambda: object())
    monkeypatch.setattr(
        article_repository_module,
        "PgVectorArticleSearchRepository",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        internal_search_module,
        "InternalSearchService",
        lambda **_kwargs: internal_search,
    )
    monkeypatch.setattr(
        planning_service_module,
        "QuestionPlanningService",
        lambda **_kwargs: object(),
    )
    delta_reporter = object()
    continuation = object()

    phases = composition._build_answering_phases(
        session_factory=cast(async_sessionmaker[AsyncSession], object()),
        delta_reporter=delta_reporter,
        continuation=continuation,
    )

    assert captured["direct"]["delta_reporter"] is delta_reporter
    assert captured["direct"]["continuation"] is continuation
    assert captured["direct"]["agent"] is DIRECT_ANSWER_AGENT
    assert (
        captured["direct"]["runtime_scope_factory"]
        is composition.activate_gemini_agent_runtime
    )
    assert captured["evidence"]["delta_reporter"] is delta_reporter
    assert captured["evidence"]["continuation"] is continuation
    assert captured["evidence"]["agent"] is EVIDENCE_ANSWER_AGENT
    assert (
        captured["evidence"]["runtime_scope_factory"]
        is composition.activate_gemini_agent_runtime
    )
    assert isinstance(phases, AnsweringPhases)
    assert phases.collector.internal_search is internal_search
    assert phases.direct_answerer is not None
    assert phases.evidence_answerer is not None
