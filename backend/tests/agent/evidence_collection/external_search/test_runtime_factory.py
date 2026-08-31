"""External search / evidence reviewer の資源scopeの境界契約。"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import SecretStr

from app.agent import composition
from app.agent.composition import (
    activate_evidence_reviewer_runtime,
    activate_external_search,
)
from app.agent.evidence_review.deepseek_binding import (
    EVIDENCE_REVIEWER_DEEPSEEK_BINDING,
)
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


class _TrackedSearchClient:
    def __init__(self, *, close_error: BaseException | None = None) -> None:
        self._close_error = close_error
        self.close_count = 0

    async def aclose(self) -> None:
        self.close_count += 1
        if self._close_error is not None:
            raise self._close_error


class _TrackedSearchClientContext:
    def __init__(
        self,
        *,
        client: _TrackedSearchClient,
        entry_error: BaseException | None = None,
    ) -> None:
        self._client = client
        self._entry_error = entry_error
        self.enter_count = 0

    async def __aenter__(self) -> _TrackedSearchClient:
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


class _TrackedSearchClientFactory:
    def __init__(
        self,
        *,
        entry_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self._entry_error = entry_error
        self._close_error = close_error
        self.clients: list[_TrackedSearchClient] = []
        self.contexts: list[_TrackedSearchClientContext] = []

    def __call__(self, **kwargs: object) -> _TrackedSearchClientContext:
        client = _TrackedSearchClient(close_error=self._close_error)
        context = _TrackedSearchClientContext(
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


class _GatewaySpy:
    def __init__(self, *, gateway_url: str, region: str, client: object) -> None:
        self.gateway_url = gateway_url
        self.region = region
        self.client = client


class _GatewaySpyFactory:
    def __init__(self, *, construction_error: BaseException | None = None) -> None:
        self._construction_error = construction_error
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> _GatewaySpy:
        self.calls.append(kwargs)
        if self._construction_error is not None:
            raise self._construction_error
        return _GatewaySpy(**kwargs)  # type: ignore[arg-type]


def _install_factory_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    deepseek: _TrackedDeepSeekClientFactory | None = None,
    search_http: _TrackedSearchClientFactory | None = None,
    runtime: _RuntimeSpyFactory | None = None,
    gateway: _GatewaySpyFactory | None = None,
) -> tuple[
    _TrackedDeepSeekClientFactory,
    _TrackedSearchClientFactory,
    _RuntimeSpyFactory,
    _GatewaySpyFactory,
]:
    import openai

    from app.agent.evidence_collection.external_search import (
        agentcore as agentcore_module,
    )
    from app.agent.runtime import deepseek as deepseek_module

    deepseek = deepseek or _TrackedDeepSeekClientFactory()
    search_http = search_http or _TrackedSearchClientFactory()
    runtime = runtime or _RuntimeSpyFactory()
    gateway = gateway or _GatewaySpyFactory()
    monkeypatch.setattr(openai, "AsyncOpenAI", deepseek)
    monkeypatch.setattr(composition, "make_internal_async_client", search_http)
    monkeypatch.setattr(deepseek_module, "DeepSeekAgentRuntime", runtime)
    monkeypatch.setattr(agentcore_module, "AgentCoreWebSearchGateway", gateway)
    monkeypatch.setattr(
        composition.settings,
        "deepseek_api_key",
        SecretStr("deepseek-api-key-sentinel"),
    )
    monkeypatch.setattr(
        composition.settings,
        "agentcore_gateway_url",
        "https://gw-sentinel.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com",
    )
    monkeypatch.setattr(composition.settings, "aws_region", "ap-northeast-1")
    return deepseek, search_http, runtime, gateway


@pytest.mark.asyncio
async def test_external_search_scope_is_lazy_and_closes_each_client_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.agent.evidence_collection.external_search.deepseek_binding import (
        EXTERNAL_QUERY_DEEPSEEK_BINDING,
    )
    from app.agent.runtime.deepseek import (
        DEEPSEEK_BASE_URL,
        DEEPSEEK_CLIENT_TIMEOUT_SECONDS,
    )

    deepseek, search_http, runtime, gateway = _install_factory_dependencies(monkeypatch)
    scope = activate_external_search()

    assert (deepseek.clients, search_http.clients, runtime.calls, gateway.calls) == (
        [],
        [],
        [],
        [],
    )

    async with scope as external_search:
        assert (
            len(deepseek.clients),
            len(search_http.clients),
            deepseek.clients[0].kwargs,
            external_search.query_runtime.client is deepseek.clients[0],
            external_search.query_runtime.binding is EXTERNAL_QUERY_DEEPSEEK_BINDING,
            external_search.search_gateway.client is search_http.clients[0],
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
        )

    assert (deepseek.clients[0].close_count, search_http.clients[0].close_count) == (
        1,
        1,
    )


@pytest.mark.asyncio
async def test_evidence_reviewer_scope_is_lazy_and_closes_its_client_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reviewerは外部検索と資源を共有せず、自分のDeepSeek clientだけを開閉する。"""
    from app.agent.runtime.deepseek import (
        DEEPSEEK_BASE_URL,
        DEEPSEEK_CLIENT_TIMEOUT_SECONDS,
    )

    deepseek, search_http, runtime, _gateway = _install_factory_dependencies(
        monkeypatch
    )
    scope = activate_evidence_reviewer_runtime()

    assert (deepseek.clients, runtime.calls) == ([], [])

    async with scope as reviewer_runtime:
        assert (
            len(deepseek.clients),
            search_http.clients,
            deepseek.clients[0].kwargs,
            reviewer_runtime.client is deepseek.clients[0],
            reviewer_runtime.binding is EVIDENCE_REVIEWER_DEEPSEEK_BINDING,
        ) == (
            1,
            [],
            {
                "api_key": "deepseek-api-key-sentinel",
                "base_url": DEEPSEEK_BASE_URL,
                "timeout": DEEPSEEK_CLIENT_TIMEOUT_SECONDS,
            },
            True,
            True,
        )

    assert deepseek.clients[0].close_count == 1


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
async def test_external_search_scope_closes_acquired_clients_for_every_exit(
    monkeypatch: pytest.MonkeyPatch,
    body_error: BaseException | None,
) -> None:
    deepseek, search_http, _runtime, _gateway = _install_factory_dependencies(
        monkeypatch
    )

    if body_error is None:
        async with activate_external_search():
            pass
    else:
        with pytest.raises(type(body_error)) as raised:
            async with activate_external_search():
                raise body_error
        assert raised.value is body_error

    assert (deepseek.clients[0].close_count, search_http.clients[0].close_count) == (
        1,
        1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "expected_deepseek_closes", "expected_search_closes"),
    [
        pytest.param("query-runtime", 1, 0, id="query-runtime"),
        pytest.param("search-http-entry", 1, 0, id="search-http-entry"),
        pytest.param("gateway", 1, 1, id="gateway"),
    ],
)
async def test_external_search_scope_closes_only_acquired_clients_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    expected_deepseek_closes: int,
    expected_search_closes: int,
) -> None:
    runtime = _RuntimeSpyFactory(
        fail_on_construction=1 if stage == "query-runtime" else None
    )
    search_http = _TrackedSearchClientFactory(
        entry_error=RuntimeError("search http entry failed")
        if stage == "search-http-entry"
        else None
    )
    gateway_error = (
        RuntimeError("gateway construction failed") if stage == "gateway" else None
    )
    deepseek, search_http, _runtime, _gateway = _install_factory_dependencies(
        monkeypatch,
        search_http=search_http,
        runtime=runtime,
        gateway=_GatewaySpyFactory(construction_error=gateway_error),
    )

    with pytest.raises(RuntimeError):
        async with activate_external_search():
            raise AssertionError("scope body must not run")

    assert (
        deepseek.clients[0].close_count,
        sum(client.close_count for client in search_http.clients),
    ) == (expected_deepseek_closes, expected_search_closes)


@pytest.mark.asyncio
async def test_external_search_scope_attempts_deepseek_close_when_http_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_error = RuntimeError("search http close failure")
    search_http = _TrackedSearchClientFactory(close_error=close_error)
    deepseek, search_http, _runtime, _gateway = _install_factory_dependencies(
        monkeypatch,
        search_http=search_http,
    )

    with pytest.raises(RuntimeError) as raised:
        async with activate_external_search():
            pass

    assert (
        raised.value is close_error,
        deepseek.clients[0].close_count,
        search_http.clients[0].close_count,
    ) == (True, 1, 1)


@pytest.mark.asyncio
async def test_external_search_scope_allows_close_failure_to_replace_body_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_error = RuntimeError("body failure")
    close_error = RuntimeError("search http close failure")
    search_http = _TrackedSearchClientFactory(close_error=close_error)
    _deepseek, _tavily, _runtime, _gateway = _install_factory_dependencies(
        monkeypatch,
        search_http=search_http,
    )

    with pytest.raises(RuntimeError) as raised:
        async with activate_external_search():
            raise body_error

    assert (raised.value is close_error, raised.value.__context__ is body_error) == (
        True,
        True,
    )


@pytest.mark.asyncio
async def test_external_search_scope_creates_fresh_clients_for_each_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deepseek, search_http, _runtime, _gateway = _install_factory_dependencies(
        monkeypatch
    )

    async with activate_external_search() as first:
        pass
    async with activate_external_search() as second:
        pass

    assert (
        first.query_runtime.client is not second.query_runtime.client,
        first.search_gateway.client is not second.search_gateway.client,
        [client.close_count for client in deepseek.clients],
        [client.close_count for client in search_http.clients],
    ) == (True, True, [1, 1], [1, 1])
