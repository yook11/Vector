"""Agent composition public builder behavior tests。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.agent import composition
from app.agent.contract import (
    AnswerQuestionInput,
    AnswerQuestionResult,
    AnswerRetrievalSummary,
)
from app.agent.question_context import QuestionContext, QuestionContextDraft
from app.agent.running import AnsweringRunner, RunContext, RunInput
from app.agent.threads.contracts import ThreadMessageSnapshot
from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderError,
)

RUN_ID = UUID("019bd239-1ed4-7fbb-a336-04fe3c197645")
AS_OF = datetime(2026, 7, 16, 9, 30, tzinfo=UTC)


class _FakeQuestionContextGenerator:
    def __init__(self, draft: QuestionContextDraft) -> None:
        self._draft = draft
        self.calls: list[dict[str, object]] = []

    async def generate(
        self,
        *,
        question: str,
        history: list[ThreadMessageSnapshot],
        as_of: datetime,
    ) -> QuestionContextDraft:
        self.calls.append(
            {
                "question": question,
                "history": history,
                "as_of": as_of,
            }
        )
        return self._draft


class _FakeQuestionAnsweringAgent:
    def __init__(
        self,
        outcomes: list[AnswerQuestionResult | BaseException],
        *,
        lifecycle: list[str] | None = None,
    ) -> None:
        self._outcomes = outcomes
        self._lifecycle = lifecycle
        self.calls: list[AnswerQuestionInput] = []

    async def answer(self, input: AnswerQuestionInput) -> AnswerQuestionResult:
        self.calls.append(input)
        if self._lifecycle is not None:
            self._lifecycle.append("concrete agent answer")
        outcome = self._outcomes[len(self.calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


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


def _composition_builder(name: str) -> Any:
    builder = getattr(composition, name, None)
    if builder is None:
        pytest.fail(
            f"app.agent.composition.{name} が未実装です",
            pytrace=False,
        )
    return builder


def _answer_result(answer: str = "最終回答") -> AnswerQuestionResult:
    return AnswerQuestionResult(
        status="answered",
        answer=answer,
        retrieval=AnswerRetrievalSummary(planned_mode="none"),
    )


def _answer_input() -> AnswerQuestionInput:
    return AnswerQuestionInput(
        context=QuestionContext(standalone_question="整理済みの質問"),
        as_of=AS_OF,
        previous_answer="前回の回答",
    )


async def test_build_answering_runner_uses_built_question_context_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_answering_runner = _composition_builder("build_answering_runner")
    question = "それが投資へ与える影響は？"
    history = (ThreadMessageSnapshot(role="assistant", content="前回の回答"),)
    generator = _FakeQuestionContextGenerator(
        QuestionContextDraft(
            standalone_question="NVIDIA の発表が投資へ与える影響は？",
            content_requirements=["株価への影響を含める"],
        )
    )
    generator_builder_calls: list[None] = []
    monkeypatch.setattr(
        composition,
        "build_question_context_generator",
        lambda: generator_builder_calls.append(None) or generator,
    )
    final_output = _answer_result()
    starting_agent = _FakeQuestionAnsweringAgent([final_output])

    answering_runner = build_answering_runner()
    result = await answering_runner.run(
        starting_agent,
        RunInput(question=question, history=history),
        run_context=RunContext(run_id=RUN_ID, as_of=AS_OF),
    )

    assert (
        isinstance(answering_runner, AnsweringRunner),
        generator_builder_calls,
        generator.calls,
        len(starting_agent.calls),
        starting_agent.calls[0].context is result.context.question_context,
        result.context.question_context.standalone_question,
        result.final_output is final_output,
    ) == (
        True,
        [None],
        [{"question": question, "history": list(history), "as_of": AS_OF}],
        1,
        True,
        "NVIDIA の発表が投資へ与える影響は？",
        True,
    )


@pytest.mark.parametrize(
    "builder_error",
    [
        pytest.param(AIProviderConfigurationError(), id="configuration-error"),
        pytest.param(AIProviderError(), id="provider-error"),
    ],
)
async def test_build_answering_runner_falls_back_for_known_generator_errors(
    monkeypatch: pytest.MonkeyPatch,
    builder_error: AIProviderError,
) -> None:
    build_answering_runner = _composition_builder("build_answering_runner")
    builder_calls: list[None] = []

    def fail_to_build_generator() -> None:
        builder_calls.append(None)
        raise builder_error

    monkeypatch.setattr(
        composition,
        "build_question_context_generator",
        fail_to_build_generator,
    )
    question = "NVIDIA の直近発表は？"
    final_output = _answer_result()
    starting_agent = _FakeQuestionAnsweringAgent([final_output])

    result = await build_answering_runner().run(
        starting_agent,
        RunInput(question=question, history=()),
        run_context=RunContext(run_id=RUN_ID, as_of=AS_OF),
    )

    assert (
        builder_calls,
        len(starting_agent.calls),
        starting_agent.calls[0].context is result.context.question_context,
        result.context.question_context.standalone_question,
        [
            requirement.description
            for requirement in result.context.question_context.content_requirements
        ],
        result.final_output is final_output,
    ) == ([None], 1, True, question, [question], True)


def test_build_answering_runner_propagates_unexpected_generator_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_answering_runner = _composition_builder("build_answering_runner")
    error = RuntimeError("unexpected builder failure")
    builder_calls: list[None] = []

    def fail_to_build_generator() -> None:
        builder_calls.append(None)
        raise error

    monkeypatch.setattr(
        composition,
        "build_question_context_generator",
        fail_to_build_generator,
    )

    with pytest.raises(RuntimeError) as raised:
        build_answering_runner()

    assert (raised.value is error, builder_calls) == (True, [None])


def test_starting_agent_factory_does_not_open_client_or_build_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_starting_agent = _composition_builder(
        "build_question_answering_starting_agent"
    )
    construction_calls: list[str] = []

    def make_client() -> None:
        construction_calls.append("make client")

    def build_graph(**_kwargs: object) -> None:
        construction_calls.append("build graph")

    monkeypatch.setattr(composition, "make_safe_async_client", make_client)
    monkeypatch.setattr(composition, "build_question_answering_agent", build_graph)

    starting_agent = build_starting_agent(session_factory=object())

    assert (construction_calls, callable(starting_agent.answer)) == ([], True)


async def test_deferred_answer_orders_resources_forwards_dependencies_and_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_starting_agent = _composition_builder(
        "build_question_answering_starting_agent"
    )
    lifecycle: list[str] = []
    client_factory = _SafeClientFactory(lifecycle)
    monkeypatch.setattr(composition, "make_safe_async_client", client_factory)
    final_output = _answer_result()
    concrete_agent = _FakeQuestionAnsweringAgent(
        [final_output],
        lifecycle=lifecycle,
    )
    graph_builder_calls: list[dict[str, object]] = []

    def build_graph(**kwargs: object) -> _FakeQuestionAnsweringAgent:
        lifecycle.append("graph build")
        graph_builder_calls.append(kwargs)
        return concrete_agent

    monkeypatch.setattr(composition, "build_question_answering_agent", build_graph)
    session_factory = object()
    progress = object()
    events = object()
    delta_reporter = object()
    continuation = object()
    starting_agent = build_starting_agent(
        session_factory=session_factory,
        progress=progress,
        events=events,
        delta_reporter=delta_reporter,
        continuation=continuation,
    )
    input_ = _answer_input()

    result = await starting_agent.answer(input_)

    assert (
        lifecycle,
        graph_builder_calls,
        len(concrete_agent.calls),
        concrete_agent.calls[0] is input_,
        result is final_output,
    ) == (
        ["client 1 enter", "graph build", "concrete agent answer", "client 1 exit"],
        [
            {
                "session_factory": session_factory,
                "tavily_client": client_factory.clients[0],
                "progress": progress,
                "events": events,
                "delta_reporter": delta_reporter,
                "continuation": continuation,
            }
        ],
        1,
        True,
        True,
    )


async def test_deferred_answer_releases_client_when_graph_builder_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_starting_agent = _composition_builder(
        "build_question_answering_starting_agent"
    )
    lifecycle: list[str] = []
    client_factory = _SafeClientFactory(lifecycle)
    monkeypatch.setattr(composition, "make_safe_async_client", client_factory)
    error = AIProviderConfigurationError()

    def fail_to_build_graph(**_kwargs: object) -> None:
        lifecycle.append("graph build")
        raise error

    monkeypatch.setattr(
        composition,
        "build_question_answering_agent",
        fail_to_build_graph,
    )
    starting_agent = build_starting_agent(session_factory=object())

    with pytest.raises(AIProviderConfigurationError) as raised:
        await starting_agent.answer(_answer_input())

    assert (
        raised.value is error,
        lifecycle,
        len(client_factory.clients),
    ) == (
        True,
        ["client 1 enter", "graph build", "client 1 exit"],
        1,
    )


async def test_deferred_answer_releases_client_when_concrete_agent_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_starting_agent = _composition_builder(
        "build_question_answering_starting_agent"
    )
    lifecycle: list[str] = []
    client_factory = _SafeClientFactory(lifecycle)
    monkeypatch.setattr(composition, "make_safe_async_client", client_factory)
    error = RuntimeError("answer failed")
    concrete_agent = _FakeQuestionAnsweringAgent([error], lifecycle=lifecycle)

    def build_graph(**_kwargs: object) -> _FakeQuestionAnsweringAgent:
        lifecycle.append("graph build")
        return concrete_agent

    monkeypatch.setattr(composition, "build_question_answering_agent", build_graph)
    starting_agent = build_starting_agent(session_factory=object())

    with pytest.raises(RuntimeError) as raised:
        await starting_agent.answer(_answer_input())

    assert (
        raised.value is error,
        lifecycle,
        len(concrete_agent.calls),
        len(client_factory.clients),
    ) == (
        True,
        ["client 1 enter", "graph build", "concrete agent answer", "client 1 exit"],
        1,
        1,
    )


async def test_deferred_agent_opens_fresh_client_for_each_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_starting_agent = _composition_builder(
        "build_question_answering_starting_agent"
    )
    lifecycle: list[str] = []
    client_factory = _SafeClientFactory(lifecycle)
    monkeypatch.setattr(composition, "make_safe_async_client", client_factory)
    first_output = _answer_result("最初の回答")
    second_output = _answer_result("次の回答")
    concrete_agents = [
        _FakeQuestionAnsweringAgent([first_output], lifecycle=lifecycle),
        _FakeQuestionAnsweringAgent([second_output], lifecycle=lifecycle),
    ]
    graph_builder_calls: list[dict[str, object]] = []

    def build_graph(**kwargs: object) -> _FakeQuestionAnsweringAgent:
        lifecycle.append("graph build")
        graph_builder_calls.append(kwargs)
        return concrete_agents[len(graph_builder_calls) - 1]

    monkeypatch.setattr(composition, "build_question_answering_agent", build_graph)
    starting_agent = build_starting_agent(session_factory=object())

    first_result = await starting_agent.answer(_answer_input())
    second_result = await starting_agent.answer(_answer_input())

    assert (
        lifecycle,
        len(client_factory.clients),
        client_factory.clients[0] is not client_factory.clients[1],
        [call["tavily_client"] for call in graph_builder_calls],
        first_result is first_output,
        second_result is second_output,
    ) == (
        [
            "client 1 enter",
            "graph build",
            "concrete agent answer",
            "client 1 exit",
            "client 2 enter",
            "graph build",
            "concrete agent answer",
            "client 2 exit",
        ],
        2,
        True,
        client_factory.clients,
        True,
        True,
    )


@pytest.mark.parametrize(
    "unexpected_argument", ["tavily_client", "http_client_factory"]
)
def test_starting_agent_factory_rejects_public_client_injection(
    unexpected_argument: str,
) -> None:
    build_starting_agent = _composition_builder(
        "build_question_answering_starting_agent"
    )

    with pytest.raises(TypeError):
        build_starting_agent(
            session_factory=object(),
            **{unexpected_argument: object()},
        )
