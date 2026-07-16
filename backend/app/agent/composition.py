"""Question-answering agent composition.

The API process only performs the lightweight configuration check; worker tasks
call the builder when they actually execute an agent run.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.contract import (
    AnswerDeltaReporter,
    AnswerEventReporter,
    AnswerGenerationContinuation,
    AnswerProgressReporter,
    AnswerQuestionInput,
    AnswerQuestionResult,
    QuestionAnsweringAgent,
)
from app.agent.evidence_collection.external_search.tavily import TavilyHttpClient
from app.agent.question_context.contract import QuestionContextGenerator
from app.agent.question_context.service import QuestionContextService
from app.agent.running import Runner
from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderError,
)
from app.config import settings
from app.shared.security.safe_http import make_safe_async_client


def ensure_question_answering_agent_configured() -> None:
    if not (
        settings.deepseek_api_key.get_secret_value()
        and settings.tavily_api_key.get_secret_value()
    ):
        raise AIProviderConfigurationError()


class _DeferredQuestionAnsweringAgent:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        progress: AnswerProgressReporter | None,
        events: AnswerEventReporter | None,
        delta_reporter: AnswerDeltaReporter | None,
        continuation: AnswerGenerationContinuation | None,
    ) -> None:
        self._session_factory = session_factory
        self._progress = progress
        self._events = events
        self._delta_reporter = delta_reporter
        self._continuation = continuation

    async def answer(self, input: AnswerQuestionInput) -> AnswerQuestionResult:
        async with make_safe_async_client() as tavily_client:
            agent = build_question_answering_agent(
                session_factory=self._session_factory,
                tavily_client=tavily_client,
                progress=self._progress,
                events=self._events,
                delta_reporter=self._delta_reporter,
                continuation=self._continuation,
            )
            return await agent.answer(input)


def build_question_answering_starting_agent(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    progress: AnswerProgressReporter | None = None,
    events: AnswerEventReporter | None = None,
    delta_reporter: AnswerDeltaReporter | None = None,
    continuation: AnswerGenerationContinuation | None = None,
) -> QuestionAnsweringAgent:
    return _DeferredQuestionAnsweringAgent(
        session_factory=session_factory,
        progress=progress,
        events=events,
        delta_reporter=delta_reporter,
        continuation=continuation,
    )


def build_question_answering_agent(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    tavily_client: TavilyHttpClient,
    progress: AnswerProgressReporter | None = None,
    events: AnswerEventReporter | None = None,
    delta_reporter: AnswerDeltaReporter | None = None,
    continuation: AnswerGenerationContinuation | None = None,
) -> QuestionAnsweringAgent:
    ensure_question_answering_agent_configured()

    from app.agent.answering.direct_answer.ai.gemini import (
        GeminiDirectAnswerGenerator,
    )
    from app.agent.answering.direct_answer.flow import DirectAnswerFlow
    from app.agent.answering.evidence_answer.ai.gemini import (
        GeminiEvidenceAnswerDraftGenerator,
    )
    from app.agent.answering.evidence_answer.flow import EvidenceAnswerFlow
    from app.agent.answering.orchestration import QuestionAnsweringOrchestrator
    from app.agent.evidence_collection import EvidenceCollectionService
    from app.agent.evidence_collection.internal_search.ai.gemini import (
        GeminiQueryEmbedder,
    )
    from app.agent.evidence_collection.internal_search.article_search import (
        PgVectorArticleSearchRepository,
    )
    from app.agent.evidence_collection.internal_search.service import (
        InternalSearchService,
    )
    from app.agent.planning.ai.gemini import GeminiQuestionPlanner
    from app.agent.planning.service import QuestionPlanningService

    external_search = _build_external_search(tavily_client, events=events)
    internal_search = InternalSearchService(
        embedder=GeminiQueryEmbedder(),
        article_search_repository=PgVectorArticleSearchRepository(session_factory),
        events=events,
    )
    return QuestionAnsweringOrchestrator(
        planner=QuestionPlanningService(
            planner=GeminiQuestionPlanner(),
            audit_recorder=None,
        ),
        evidence_collector=EvidenceCollectionService(
            internal_search=internal_search,
            external_search=external_search,
            requested_external_agent_count=None,
        ),
        evidence_answerer=EvidenceAnswerFlow(
            generator=GeminiEvidenceAnswerDraftGenerator(),
            audit_recorder=None,
            delta_reporter=delta_reporter,
            continuation=continuation,
        ),
        direct_answerer=DirectAnswerFlow(
            generator=GeminiDirectAnswerGenerator(),
            audit_recorder=None,
            delta_reporter=delta_reporter,
            continuation=continuation,
        ),
        progress=progress,
    )


def build_question_context_generator() -> QuestionContextGenerator:
    """Build the worker-owned generator without coupling agent core to history."""

    from app.agent.question_context.ai.gemini import GeminiQuestionContextGenerator

    return GeminiQuestionContextGenerator()


def build_runner() -> Runner:
    try:
        generator = build_question_context_generator()
    except (AIProviderConfigurationError, AIProviderError):
        generator = None
    return Runner(
        context_preparer=QuestionContextService(generator=generator),
    )


def _build_external_search(
    tavily_client: TavilyHttpClient,
    *,
    events: AnswerEventReporter | None = None,
) -> object:
    ensure_question_answering_agent_configured()

    from app.agent.evidence_collection.external_search.ai.deepseek import (
        DeepSeekEvidenceSelector,
        DeepSeekQueryGenerator,
    )
    from app.agent.evidence_collection.external_search.runner import (
        ExternalSearchResearchRunner,
    )
    from app.agent.evidence_collection.external_search.service import (
        ExternalSearchService,
    )
    from app.agent.evidence_collection.external_search.tavily import (
        TavilySearchProvider,
    )

    return ExternalSearchService(
        runner=ExternalSearchResearchRunner(
            query_generator=DeepSeekQueryGenerator(),
            search_provider=TavilySearchProvider(
                api_key=settings.tavily_api_key,
                client=tavily_client,
            ),
            evidence_selector=DeepSeekEvidenceSelector(),
            events=events,
        )
    )
