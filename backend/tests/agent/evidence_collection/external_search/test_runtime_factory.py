"""External research runtime factory の境界契約。"""

from __future__ import annotations

import asyncio
from dataclasses import fields, is_dataclass
from typing import Any

import pytest
from pydantic import SecretStr

from app.agent import composition
from app.agent.runtime.contract import AgentResponseDefect, AgentResponseInvalidError


class _TrackedDeepSeekClient:
    def __init__(
        self,
        *,
        kwargs: dict[str, object],
        close_error: BaseException | None = None,
    ) -> None:
        self.kwargs = kwargs
        self._close_error = close_error
        self.enter_count = 0
        self.close_count = 0

    async def __aenter__(self) -> _TrackedDeepSeekClient:
        self.enter_count += 1
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> bool:
        await self.close()
        return False

    async def close(self) -> None:
        self.close_count += 1
        if self._close_error is not None:
            raise self._close_error


class _TrackedDeepSeekClientFactory:
    def __init__(self, *, close_error: BaseException | None = None) -> None:
        self._close_error = close_error
        self.clients: list[_TrackedDeepSeekClient] = []

    def __call__(self, **kwargs: object) -> _TrackedDeepSeekClient:
        client = _TrackedDeepSeekClient(
            kwargs=kwargs,
            close_error=self._close_error,
        )
        self.clients.append(client)
        return client


class _TrackedTavilyClient:
    def __init__(self, *, close_error: BaseException | None = None) -> None:
        self._close_error = close_error
        self.close_count = 0

    async def aclose(self) -> None:
        self.close_count += 1
        if self._close_error is not None:
            raise self._close_error


class _TrackedTavilyContext:
    def __init__(
        self,
        *,
        client: _TrackedTavilyClient,
        entry_error: BaseException | None = None,
    ) -> None:
        self._client = client
        self._entry_error = entry_error
        self.enter_count = 0

    async def __aenter__(self) -> _TrackedTavilyClient:
        self.enter_count += 1
        if self._entry_error is not None:
            raise self._entry_error
        return self._client

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> bool:
        await self._client.aclose()
        return False


class _TrackedTavilyClientFactory:
    def __init__(
        self,
        *,
        entry_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self._entry_error = entry_error
        self._close_error = close_error
        self.clients: list[_TrackedTavilyClient] = []
        self.contexts: list[_TrackedTavilyContext] = []

    def __call__(self) -> _TrackedTavilyContext:
        client = _TrackedTavilyClient(close_error=self._close_error)
        context = _TrackedTavilyContext(
            client=client,
            entry_error=self._entry_error,
        )
        self.clients.append(client)
        self.contexts.append(context)
        return context


class _RuntimeSpy:
    def __init__(self, *, client: object, binding: object) -> None:
        self.client = client
        self.binding = binding


class _RuntimeSpyFactory:
    def __init__(self, *, fail_on_construction: int | None = None) -> None:
        self._fail_on_construction = fail_on_construction
        self.calls: list[tuple[object, object]] = []

    def __call__(self, *, client: object, binding: object) -> _RuntimeSpy:
        self.calls.append((client, binding))
        if len(self.calls) == self._fail_on_construction:
            raise RuntimeError(f"runtime construction {len(self.calls)} failed")
        return _RuntimeSpy(client=client, binding=binding)


class _ToolSpy:
    def __init__(self, *, api_key: SecretStr, client: object) -> None:
        self.api_key = api_key
        self.client = client


class _ToolSpyFactory:
    def __init__(self, *, construction_error: BaseException | None = None) -> None:
        self._construction_error = construction_error
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> _ToolSpy:
        self.calls.append(kwargs)
        if self._construction_error is not None:
            raise self._construction_error
        return _ToolSpy(**kwargs)  # type: ignore[arg-type]


def _factory_builder() -> Any:
    builder = getattr(composition, "build_external_research_runtime_factory", None)
    if builder is None:
        pytest.fail(
            "app.agent.composition."
            "build_external_research_runtime_factory が未実装です",
            pytrace=False,
        )
    return builder


def _install_factory_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    deepseek: _TrackedDeepSeekClientFactory | None = None,
    tavily: _TrackedTavilyClientFactory | None = None,
    runtime: _RuntimeSpyFactory | None = None,
    tool: _ToolSpyFactory | None = None,
) -> tuple[
    _TrackedDeepSeekClientFactory,
    _TrackedTavilyClientFactory,
    _RuntimeSpyFactory,
    _ToolSpyFactory,
]:
    import openai

    from app.agent.evidence_collection.external_search import tavily as tavily_module
    from app.agent.runtime import deepseek as deepseek_module

    deepseek = deepseek or _TrackedDeepSeekClientFactory()
    tavily = tavily or _TrackedTavilyClientFactory()
    runtime = runtime or _RuntimeSpyFactory()
    tool = tool or _ToolSpyFactory()
    monkeypatch.setattr(openai, "AsyncOpenAI", deepseek)
    monkeypatch.setattr(composition, "make_safe_async_client", tavily)
    monkeypatch.setattr(deepseek_module, "DeepSeekAgentRuntime", runtime)
    monkeypatch.setattr(tavily_module, "TavilyExternalSearchTool", tool)
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
    return deepseek, tavily, runtime, tool


def test_external_research_runtime_contract_declares_only_borrowed_resources() -> None:
    from app.agent.evidence_collection.external_search.contract import (
        ExternalResearchRuntime,
        ExternalResearchRuntimeFactory,
    )

    assert (
        is_dataclass(ExternalResearchRuntime),
        ExternalResearchRuntime.__dataclass_params__.frozen,
        tuple(field.name for field in fields(ExternalResearchRuntime)),
        callable(getattr(ExternalResearchRuntimeFactory, "activate", None)),
    ) == (True, True, ("query_runtime", "selector_runtime", "search_tool"), True)


@pytest.mark.asyncio
async def test_runtime_factory_is_lazy_shares_deepseek_and_closes_each_client_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.agent.evidence_collection.external_search.deepseek_binding import (
        EXTERNAL_EVIDENCE_SELECTOR_DEEPSEEK_BINDING,
        EXTERNAL_QUERY_DEEPSEEK_BINDING,
    )
    from app.agent.runtime.deepseek import (
        DEEPSEEK_BASE_URL,
        DEEPSEEK_CLIENT_TIMEOUT_SECONDS,
    )

    deepseek, tavily, runtime, tool = _install_factory_dependencies(monkeypatch)
    factory = _factory_builder()()
    scope = factory.activate()

    assert (deepseek.clients, tavily.clients, runtime.calls, tool.calls) == (
        [],
        [],
        [],
        [],
    )

    async with scope as external:
        assert (
            len(deepseek.clients),
            len(tavily.clients),
            deepseek.clients[0].kwargs,
            external.query_runtime.client is deepseek.clients[0],
            external.selector_runtime.client is deepseek.clients[0],
            external.query_runtime.binding is EXTERNAL_QUERY_DEEPSEEK_BINDING,
            external.selector_runtime.binding
            is EXTERNAL_EVIDENCE_SELECTOR_DEEPSEEK_BINDING,
            external.search_tool.client is tavily.clients[0],
        ) == (
            1,
            1,
            {
                "api_key": "deepseek-api-key-sentinel",
                "base_url": DEEPSEEK_BASE_URL,
                "timeout": DEEPSEEK_CLIENT_TIMEOUT_SECONDS,
            },
            True,
            True,
            True,
            True,
            True,
        )

    assert (deepseek.clients[0].close_count, tavily.clients[0].close_count) == (1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body_error",
    [
        pytest.param(None, id="normal"),
        pytest.param(
            AgentResponseInvalidError(AgentResponseDefect.RESPONSE_NOT_JSON),
            id="classified-failure",
        ),
        pytest.param(RuntimeError("unclassified body failure"), id="unexpected"),
        pytest.param(asyncio.CancelledError(), id="cancellation"),
    ],
)
async def test_runtime_factory_closes_acquired_clients_for_every_scope_exit(
    monkeypatch: pytest.MonkeyPatch,
    body_error: BaseException | None,
) -> None:
    deepseek, tavily, _runtime, _tool = _install_factory_dependencies(monkeypatch)
    factory = _factory_builder()()

    if body_error is None:
        async with factory.activate():
            pass
    else:
        with pytest.raises(type(body_error)) as raised:
            async with factory.activate():
                raise body_error
        assert raised.value is body_error

    assert (deepseek.clients[0].close_count, tavily.clients[0].close_count) == (1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "expected_deepseek_closes", "expected_tavily_closes"),
    [
        pytest.param("query-runtime", 1, 0, id="query-runtime"),
        pytest.param("selector-runtime", 1, 0, id="selector-runtime"),
        pytest.param("tavily-entry", 1, 0, id="tavily-entry"),
        pytest.param("tool", 1, 1, id="tool"),
    ],
)
async def test_runtime_factory_closes_only_acquired_clients_when_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    expected_deepseek_closes: int,
    expected_tavily_closes: int,
) -> None:
    runtime = _RuntimeSpyFactory(
        fail_on_construction={"query-runtime": 1, "selector-runtime": 2}.get(stage)
    )
    tavily = _TrackedTavilyClientFactory(
        entry_error=RuntimeError("tavily entry failed")
        if stage == "tavily-entry"
        else None
    )
    tool_error = RuntimeError("tool construction failed") if stage == "tool" else None
    deepseek, tavily, _runtime, _tool = _install_factory_dependencies(
        monkeypatch,
        tavily=tavily,
        runtime=runtime,
        tool=_ToolSpyFactory(construction_error=tool_error),
    )
    factory = _factory_builder()()

    with pytest.raises(RuntimeError):
        async with factory.activate():
            raise AssertionError("scope body must not run")

    assert (
        deepseek.clients[0].close_count,
        sum(client.close_count for client in tavily.clients),
    ) == (expected_deepseek_closes, expected_tavily_closes)


@pytest.mark.asyncio
async def test_runtime_factory_attempts_deepseek_close_when_tavily_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_error = RuntimeError("tavily close failure")
    tavily = _TrackedTavilyClientFactory(close_error=close_error)
    deepseek, tavily, _runtime, _tool = _install_factory_dependencies(
        monkeypatch,
        tavily=tavily,
    )
    factory = _factory_builder()()

    with pytest.raises(RuntimeError) as raised:
        async with factory.activate():
            pass

    assert (
        raised.value is close_error,
        deepseek.clients[0].close_count,
        tavily.clients[0].close_count,
    ) == (True, 1, 1)


@pytest.mark.asyncio
async def test_runtime_factory_allows_close_failure_to_replace_body_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_error = RuntimeError("body failure")
    close_error = RuntimeError("tavily close failure")
    tavily = _TrackedTavilyClientFactory(close_error=close_error)
    _deepseek, _tavily, _runtime, _tool = _install_factory_dependencies(
        monkeypatch,
        tavily=tavily,
    )
    factory = _factory_builder()()

    with pytest.raises(RuntimeError) as raised:
        async with factory.activate():
            raise body_error

    assert (raised.value is close_error, raised.value.__context__ is body_error) == (
        True,
        True,
    )


@pytest.mark.asyncio
async def test_runtime_factory_creates_fresh_clients_for_each_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deepseek, tavily, _runtime, _tool = _install_factory_dependencies(monkeypatch)
    factory = _factory_builder()()

    async with factory.activate() as first:
        pass
    async with factory.activate() as second:
        pass

    assert (
        first.query_runtime.client is not second.query_runtime.client,
        first.search_tool.client is not second.search_tool.client,
        [client.close_count for client in deepseek.clients],
        [client.close_count for client in tavily.clients],
    ) == (True, True, [1, 1], [1, 1])
