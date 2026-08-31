"""Question-answering workflow composition.

The API process only performs the lightweight configuration check; worker tasks
call the builder when they actually execute an agent run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.answering.direct_answer.agent import DIRECT_ANSWER_AGENT
from app.agent.answering.evidence_answer.agent import EVIDENCE_ANSWER_AGENT
from app.agent.contract import (
    AnswerDeltaReporter,
    AnswerEventReporter,
    AnswerGenerationContinuation,
    AnswerProgressReporter,
)
from app.agent.evidence_collection import EvidenceCollectionService
from app.agent.evidence_collection.external_search.contract import ExternalSearch
from app.agent.evidence_review import EvidenceReviewer
from app.agent.planning.agent import QUESTION_PLANNER_AGENT
from app.agent.research_handoff.agent import RESEARCH_HANDOFF_AGENT
from app.agent.running import AnsweringPhases, AnsweringRunner
from app.agent.runtime.contract import AgentRuntime
from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
)
from app.config import settings
from app.shared.http.external import make_external_async_client

if TYPE_CHECKING:
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

    from app.agent.answering.direct_answer.service import DirectAnswerService
    from app.agent.answering.evidence_answer.service import EvidenceAnswerService
    from app.agent.evidence_collection.internal_search.ai.gemini import (
        GeminiQueryEmbedder,
    )
    from app.agent.evidence_collection.internal_search.ai.gemini_spec import (
        GEMINI_QUERY_EMBEDDING_SPEC,
        embedder_identity_of,
    )
    from app.agent.evidence_collection.internal_search.article_repository import (
        PgVectorArticleSearchRepository,
    )
    from app.agent.evidence_collection.internal_search.query_embedding_cache import (
        TransactionalQueryEmbeddingCache,
    )
    from app.agent.evidence_collection.internal_search.service import (
        InternalSearchService,
    )
    from app.agent.planning.service import QuestionPlanningService
    from app.agent.research_handoff.service import ResearchHandoffService

    internal_search = InternalSearchService(
        embedder=GeminiQueryEmbedder(),
        article_search_repository=PgVectorArticleSearchRepository(session_factory),
        query_embedding_cache=TransactionalQueryEmbeddingCache(
            session_factory=session_factory,
            embedder_identity=embedder_identity_of(GEMINI_QUERY_EMBEDDING_SPEC),
        ),
    )
    return AnsweringPhases(
        planner=QuestionPlanningService(
            agent=QUESTION_PLANNER_AGENT,
            runtime_scope_factory=activate_gemini_agent_runtime,
        ),
        collector=EvidenceCollectionService(
            internal_search=internal_search,
            events=events,
            external_search_scope_factory=activate_external_search,
        ),
        reviewer=EvidenceReviewer(
            runtime_scope_factory=activate_evidence_reviewer_runtime,
        ),
        direct_answerer=DirectAnswerService(
            agent=DIRECT_ANSWER_AGENT,
            runtime_scope_factory=activate_gemini_agent_runtime,
            delta_reporter=delta_reporter,
            continuation=continuation,
        ),
        evidence_answerer=EvidenceAnswerService(
            agent=EVIDENCE_ANSWER_AGENT,
            runtime_scope_factory=activate_gemini_agent_runtime,
            delta_reporter=delta_reporter,
            continuation=continuation,
        ),
        organizer=ResearchHandoffService(
            agent=RESEARCH_HANDOFF_AGENT,
            runtime_scope_factory=activate_gemini_agent_runtime,
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
    return AnsweringRunner(
        phases_factory=lambda: _build_answering_phases(
            session_factory=session_factory,
            events=events,
            delta_reporter=delta_reporter,
            continuation=continuation,
        ),
        progress=progress,
        events=events,
    )


@asynccontextmanager
async def activate_external_search() -> AsyncIterator[ExternalSearch]:
    from openai import AsyncOpenAI

    from app.agent.evidence_collection.external_search.deepseek_binding import (
        EXTERNAL_QUERY_DEEPSEEK_BINDING,
    )
    from app.agent.evidence_collection.external_search.service import (
        ExternalSearchService,
    )
    from app.agent.evidence_collection.external_search.tavily import (
        TavilyExternalSearchGateway,
    )
    from app.agent.runtime.deepseek import (
        DEEPSEEK_BASE_URL,
        DEEPSEEK_CLIENT_TIMEOUT_SECONDS,
        DeepSeekAgentRuntime,
    )

    async with AsyncOpenAI(
        api_key=settings.deepseek_api_key.get_secret_value(),
        base_url=DEEPSEEK_BASE_URL,
        timeout=DEEPSEEK_CLIENT_TIMEOUT_SECONDS,
    ) as deepseek_client:
        query_runtime = DeepSeekAgentRuntime(
            client=deepseek_client,
            binding=EXTERNAL_QUERY_DEEPSEEK_BINDING,
        )
        async with make_external_async_client() as tavily_client:
            yield ExternalSearchService(
                query_runtime=query_runtime,
                search_gateway=TavilyExternalSearchGateway(
                    api_key=settings.tavily_api_key,
                    client=tavily_client,
                ),
            )


@asynccontextmanager
async def activate_evidence_reviewer_runtime() -> AsyncIterator[AgentRuntime]:
    from openai import AsyncOpenAI

    from app.agent.evidence_review.deepseek_binding import (
        EVIDENCE_REVIEWER_DEEPSEEK_BINDING,
    )
    from app.agent.runtime.deepseek import (
        DEEPSEEK_BASE_URL,
        DEEPSEEK_CLIENT_TIMEOUT_SECONDS,
        DeepSeekAgentRuntime,
    )

    async with AsyncOpenAI(
        api_key=settings.deepseek_api_key.get_secret_value(),
        base_url=DEEPSEEK_BASE_URL,
        timeout=DEEPSEEK_CLIENT_TIMEOUT_SECONDS,
    ) as deepseek_client:
        yield DeepSeekAgentRuntime(
            client=deepseek_client,
            binding=EVIDENCE_REVIEWER_DEEPSEEK_BINDING,
        )
