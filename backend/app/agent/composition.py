"""Question-answering workflow composition.

The API process only performs the lightweight configuration check; worker tasks
call the builder when they actually execute an agent run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.answering.direct_answer.agent import DIRECT_ANSWER_AGENT
from app.agent.answering.evidence_answer.agent import EVIDENCE_ANSWER_AGENT
from app.agent.contract import (
    AnswerDeltaReporter,
    AnswerEventReporter,
    AnswerGenerationContinuation,
    AnswerProgressReporter,
)
from app.agent.evidence_collection.external_search.contract import (
    ExternalResearchRuntime,
    ExternalResearchRuntimeFactory,
)
from app.agent.planning.agent import QUESTION_PLANNER_AGENT
from app.agent.question_context.agent import QUESTION_CONTEXT_AGENT
from app.agent.question_context.service import QuestionContextService
from app.agent.running import AnsweringPhases, AnsweringRunner
from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
)
from app.config import settings
from app.shared.security.safe_http import make_safe_async_client

if TYPE_CHECKING:
    from app.agent.evidence_collection.external_search.service import (
        ExternalSearchService,
    )
    from app.agent.runtime.gemini import GeminiAgentRuntime


def ensure_external_search_configured() -> None:
    if not (
        settings.deepseek_api_key.get_secret_value()
        and settings.tavily_api_key.get_secret_value()
    ):
        raise AIProviderConfigurationError()


@asynccontextmanager
async def activate_gemini_agent_runtime() -> AsyncIterator[GeminiAgentRuntime]:
    api_key = settings.gemini_api_key.get_secret_value()
    if not api_key:
        from app.analysis.gemini_error_translator import GeminiStateReason

        raise AIProviderConfigurationError(reason=GeminiStateReason.NOT_CONFIGURED)

    from google import genai

    from app.agent.runtime.gemini import GeminiAgentRuntime

    async with genai.Client(api_key=api_key).aio as client:
        runtime = GeminiAgentRuntime(client=client)
        yield runtime


def _build_answering_phases(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    events: AnswerEventReporter | None = None,
    delta_reporter: AnswerDeltaReporter | None = None,
    continuation: AnswerGenerationContinuation | None = None,
) -> AnsweringPhases:
    ensure_external_search_configured()

    from app.agent.answering.direct_answer.flow import DirectAnswerFlow
    from app.agent.answering.evidence_answer.flow import EvidenceAnswerFlow
    from app.agent.evidence_collection.internal_search.ai.gemini import (
        GeminiQueryEmbedder,
    )
    from app.agent.evidence_collection.internal_search.article_search import (
        PgVectorArticleSearchRepository,
    )
    from app.agent.evidence_collection.internal_search.service import (
        InternalSearchService,
    )
    from app.agent.planning.service import QuestionPlanningService

    external_search = build_external_search_service(events=events)
    external_runtime_factory = build_external_research_runtime_factory()
    internal_search = InternalSearchService(
        embedder=GeminiQueryEmbedder(),
        article_search_repository=PgVectorArticleSearchRepository(session_factory),
        events=events,
    )
    return AnsweringPhases(
        planner=QuestionPlanningService(
            agent=QUESTION_PLANNER_AGENT,
            runtime_scope_factory=activate_gemini_agent_runtime,
        ),
        internal_search=internal_search,
        external_search=external_search,
        external_runtime_factory=external_runtime_factory,
        direct_answerer=DirectAnswerFlow(
            agent=DIRECT_ANSWER_AGENT,
            runtime_scope_factory=activate_gemini_agent_runtime,
            delta_reporter=delta_reporter,
            continuation=continuation,
        ),
        evidence_answerer=EvidenceAnswerFlow(
            agent=EVIDENCE_ANSWER_AGENT,
            runtime_scope_factory=activate_gemini_agent_runtime,
            delta_reporter=delta_reporter,
            continuation=continuation,
        ),
    )


def build_answering_runner(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    progress: AnswerProgressReporter | None = None,
    events: AnswerEventReporter | None = None,
    delta_reporter: AnswerDeltaReporter | None = None,
    continuation: AnswerGenerationContinuation | None = None,
) -> AnsweringRunner:
    question_context_runtime_factory = (
        activate_gemini_agent_runtime
        if settings.gemini_api_key.get_secret_value()
        else None
    )
    return AnsweringRunner(
        context_preparer=QuestionContextService(
            agent=QUESTION_CONTEXT_AGENT,
            runtime_scope_factory=question_context_runtime_factory,
        ),
        phases_factory=lambda: _build_answering_phases(
            session_factory=session_factory,
            events=events,
            delta_reporter=delta_reporter,
            continuation=continuation,
        ),
        progress=progress,
    )


class _ExternalResearchRuntimeFactory:
    __slots__ = ("_deepseek_api_key", "_tavily_api_key")

    def __init__(
        self,
        *,
        deepseek_api_key: SecretStr,
        tavily_api_key: SecretStr,
    ) -> None:
        self._deepseek_api_key = deepseek_api_key
        self._tavily_api_key = tavily_api_key

    @asynccontextmanager
    async def activate(self) -> AsyncIterator[ExternalResearchRuntime]:
        from openai import AsyncOpenAI

        from app.agent.evidence_collection.external_search.deepseek_binding import (
            EXTERNAL_EVIDENCE_SELECTOR_DEEPSEEK_BINDING,
            EXTERNAL_QUERY_DEEPSEEK_BINDING,
        )
        from app.agent.evidence_collection.external_search.tavily import (
            TavilyExternalSearchTool,
        )
        from app.agent.runtime.deepseek import (
            DEEPSEEK_BASE_URL,
            DEEPSEEK_CLIENT_TIMEOUT_SECONDS,
            DeepSeekAgentRuntime,
        )

        async with AsyncOpenAI(
            api_key=self._deepseek_api_key.get_secret_value(),
            base_url=DEEPSEEK_BASE_URL,
            timeout=DEEPSEEK_CLIENT_TIMEOUT_SECONDS,
        ) as deepseek_client:
            query_runtime = DeepSeekAgentRuntime(
                client=deepseek_client,
                binding=EXTERNAL_QUERY_DEEPSEEK_BINDING,
            )
            selector_runtime = DeepSeekAgentRuntime(
                client=deepseek_client,
                binding=EXTERNAL_EVIDENCE_SELECTOR_DEEPSEEK_BINDING,
            )
            async with make_safe_async_client() as tavily_client:
                search_tool = TavilyExternalSearchTool(
                    api_key=self._tavily_api_key,
                    client=tavily_client,
                )
                yield ExternalResearchRuntime(
                    query_runtime=query_runtime,
                    selector_runtime=selector_runtime,
                    search_tool=search_tool,
                )


def build_external_research_runtime_factory() -> ExternalResearchRuntimeFactory:
    return _ExternalResearchRuntimeFactory(
        deepseek_api_key=settings.deepseek_api_key,
        tavily_api_key=settings.tavily_api_key,
    )


def build_external_search_service(
    *,
    events: AnswerEventReporter | None = None,
) -> ExternalSearchService:
    ensure_external_search_configured()

    from app.agent.evidence_collection.external_search.runner import (
        ExternalSearchResearchRunner,
    )
    from app.agent.evidence_collection.external_search.service import (
        ExternalSearchService,
    )

    return ExternalSearchService(
        runner=ExternalSearchResearchRunner(
            events=events,
        ),
    )
