"""Research response API router."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.answering.direct import DirectAnswerInvalidError
from app.agent.contract import AnswerQuestionInput, QuestionAnsweringAgent
from app.agent.external_search.tavily import TavilyHttpClient
from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderError,
)
from app.config import settings
from app.dependencies import CurrentUser, get_current_user, get_session
from app.schemas.research import ResearchQuestionRequest, ResearchResponse
from app.shared.security.safe_http import make_safe_async_client

router = APIRouter(prefix="/api/v1/research", tags=["research"])

_GENERATION_UNAVAILABLE_DETAIL = "Answer generation is temporarily unavailable"


async def get_tavily_http_client() -> AsyncGenerator[TavilyHttpClient]:
    async with make_safe_async_client() as client:
        yield client


def get_question_answering_agent(
    session: Annotated[AsyncSession, Depends(get_session)],
    tavily_client: Annotated[TavilyHttpClient, Depends(get_tavily_http_client)],
) -> QuestionAnsweringAgent:
    try:
        return _build_question_answering_agent(
            session=session,
            tavily_client=tavily_client,
        )
    except AIProviderError as exc:
        raise _generation_unavailable() from exc


def _build_question_answering_agent(
    *,
    session: AsyncSession,
    tavily_client: TavilyHttpClient,
) -> QuestionAnsweringAgent:
    from app.agent.answering.ai.gemini import GeminiEvidenceAnswerDraftGenerator
    from app.agent.answering.ai.gemini_direct import GeminiDirectAnswerGenerator
    from app.agent.answering.direct import DirectAnswerService
    from app.agent.answering.service import QuestionAnsweringService
    from app.agent.answering.synthesis import AnswerSynthesisService
    from app.agent.evidence_collection import EvidenceCollectionService
    from app.agent.internal_retrieval.ai.gemini import GeminiQueryEmbedder
    from app.agent.internal_retrieval.article_search import (
        PgVectorArticleSearchRepository,
    )
    from app.agent.internal_retrieval.service import InternalSearchService
    from app.agent.planning.ai.gemini import GeminiQuestionPlanner
    from app.agent.planning.service import QuestionPlanningService

    external_search = _build_external_search(tavily_client)
    internal_search = InternalSearchService(
        embedder=GeminiQueryEmbedder(),
        article_search_repository=PgVectorArticleSearchRepository(session),
    )
    return QuestionAnsweringService(
        planner=QuestionPlanningService(
            planner=GeminiQuestionPlanner(),
            audit_recorder=None,
        ),
        evidence_collector=EvidenceCollectionService(
            internal_search=internal_search,
            external_search=external_search,
            requested_external_agent_count=None,
        ),
        synthesizer=AnswerSynthesisService(
            generator=GeminiEvidenceAnswerDraftGenerator(),
            audit_recorder=None,
        ),
        direct_answerer=DirectAnswerService(
            generator=GeminiDirectAnswerGenerator(),
            audit_recorder=None,
        ),
    )


def _build_external_search(tavily_client: TavilyHttpClient) -> object:
    if not (
        settings.deepseek_api_key.get_secret_value()
        and settings.tavily_api_key.get_secret_value()
    ):
        raise AIProviderConfigurationError()

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
        )
    )


@router.post(
    "/responses",
    operation_id="create_research_response",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Answer generation is temporarily unavailable"
        }
    },
)
async def create_research_response(
    body: ResearchQuestionRequest,
    _user: Annotated[CurrentUser, Depends(get_current_user)],
    agent: Annotated[QuestionAnsweringAgent, Depends(get_question_answering_agent)],
) -> ResearchResponse:
    try:
        result = await agent.answer(
            AnswerQuestionInput(
                question=body.question,
                as_of=datetime.now(UTC),
            )
        )
    except (AIProviderError, DirectAnswerInvalidError) as exc:
        raise _generation_unavailable() from exc
    return ResearchResponse.from_result(result)


def _generation_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=_GENERATION_UNAVAILABLE_DETAIL,
    )
