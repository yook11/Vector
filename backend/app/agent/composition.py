"""Question-answering agent composition.

The API process only performs the lightweight configuration check; worker tasks
call the builder when they actually execute an agent run.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.contract import (
    AnswerEventReporter,
    AnswerProgressReporter,
    QuestionAnsweringAgent,
)
from app.agent.external_search.tavily import TavilyHttpClient
from app.agent.question_resolution.contract import QuestionResolver
from app.analysis.ai_provider_errors import AIProviderConfigurationError
from app.config import settings


def ensure_question_answering_agent_configured() -> None:
    if not (
        settings.deepseek_api_key.get_secret_value()
        and settings.tavily_api_key.get_secret_value()
    ):
        raise AIProviderConfigurationError()


def build_question_answering_agent(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    tavily_client: TavilyHttpClient,
    progress: AnswerProgressReporter | None = None,
    events: AnswerEventReporter | None = None,
) -> QuestionAnsweringAgent:
    ensure_question_answering_agent_configured()

    from app.agent.answering.direct_answer.ai.gemini import (
        GeminiDirectAnswerGenerator,
    )
    from app.agent.answering.direct_answer.pipeline import DirectAnswerPipeline
    from app.agent.answering.evidence_answer.ai.gemini import (
        GeminiEvidenceAnswerDraftGenerator,
    )
    from app.agent.answering.evidence_answer.pipeline import EvidenceAnswerPipeline
    from app.agent.answering.orchestration import QuestionAnsweringOrchestrator
    from app.agent.evidence_collection import EvidenceCollectionService
    from app.agent.internal_retrieval.ai.gemini import GeminiQueryEmbedder
    from app.agent.internal_retrieval.article_search import (
        PgVectorArticleSearchRepository,
    )
    from app.agent.internal_retrieval.service import InternalSearchService
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
        evidence_answerer=EvidenceAnswerPipeline(
            generator=GeminiEvidenceAnswerDraftGenerator(),
            audit_recorder=None,
        ),
        direct_answerer=DirectAnswerPipeline(
            generator=GeminiDirectAnswerGenerator(),
            audit_recorder=None,
        ),
        progress=progress,
    )


def build_question_resolver() -> QuestionResolver:
    """Build the worker-owned resolver without coupling agent core to history."""

    from app.agent.question_resolution.ai.gemini import GeminiQuestionResolver

    return GeminiQuestionResolver()


def _build_external_search(
    tavily_client: TavilyHttpClient,
    *,
    events: AnswerEventReporter | None = None,
) -> object:
    ensure_question_answering_agent_configured()

    from app.agent.external_search.ai.deepseek import (
        DeepSeekEvidenceSelector,
        DeepSeekQueryGenerator,
    )
    from app.agent.external_search.runner import ExternalSearchResearchRunner
    from app.agent.external_search.service import ExternalSearchService
    from app.agent.external_search.tavily import TavilySearchProvider

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
