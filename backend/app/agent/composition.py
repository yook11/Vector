"""Question-answering workflow composition.

The agent worker checks its configuration at startup; worker tasks call the
builder when they actually execute an agent run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.answering.direct_answer.agent import DIRECT_ANSWER_AGENT
from app.agent.answering.evidence_answer.agent import EVIDENCE_ANSWER_AGENT
from app.agent.contract import (
    AnswerDeltaReporter,
    AnswerEventReporter,
    AnswerProgressReporter,
)
from app.agent.evidence_collection import EvidenceCollectionService
from app.agent.evidence_collection.external_search.contract import ExternalSearch
from app.agent.evidence_review.agent import EVIDENCE_REVIEWER_AGENT
from app.agent.planning.agent import QUESTION_PLANNER_AGENT
from app.agent.research_handoff.agent import RESEARCH_HANDOFF_AGENT
from app.agent.running import AnsweringPhases, AnsweringRunner
from app.agent.running.answer_generation import (
    AnswerGenerationRepository,
)
from app.ai_providers.errors import (
    AIProviderNotSentError,
    AIProviderNotSentReason,
)
from app.ai_providers.gemini.settings import GeminiConnectionSettings
from app.config import settings
from app.http.internal import make_internal_async_client

if TYPE_CHECKING:
    from google.genai.client import AsyncClient

    from app.agent.runtime.gemini import GeminiAgentRuntime

# 工程ごとの打ち切りが先に効くよう、1回の試行の上限はそれより長くする。
_GEMINI_CONNECTION = GeminiConnectionSettings(read_timeout=30.0)


def ensure_agent_worker_configured() -> None:
    """run が使う外部接続の設定を worker の起動時に確かめ、欠けていれば起動させない。"""
    missing = [
        name
        for name, value in (
            ("GEMINI_API_KEY", settings.gemini_api_key.get_secret_value()),
            ("AGENTCORE_GATEWAY_URL", settings.agentcore_gateway_url),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"agent worker requires {', '.join(missing)}")


@asynccontextmanager
async def activate_gemini_client() -> AsyncIterator[AsyncClient]:
    if not settings.gemini_api_key.get_secret_value():
        raise AIProviderNotSentError(reason=AIProviderNotSentReason.NOT_CONFIGURED)

    from app.ai_providers.gemini.client import open_gemini_client

    async with open_gemini_client(
        api_key=settings.gemini_api_key, settings=_GEMINI_CONNECTION
    ) as client:
        yield client


@asynccontextmanager
async def activate_gemini_agent_runtime() -> AsyncIterator[GeminiAgentRuntime]:
    from app.agent.runtime.gemini import GeminiAgentRuntime

    async with activate_gemini_client() as client:
        yield GeminiAgentRuntime(client=client)


def _build_answering_phases(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    repository: AnswerGenerationRepository,
    schedule_deadline_check: Callable[[UUID, datetime], None],
    events: AnswerEventReporter | None = None,
    delta_reporter: AnswerDeltaReporter | None = None,
    progress: AnswerProgressReporter | None = None,
) -> AnsweringPhases:
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
    from app.agent.evidence_review import EvidenceReviewService
    from app.agent.planning.service import QuestionPlanningService
    from app.agent.research_handoff.service import ResearchHandoffService

    internal_search = InternalSearchService(
        embedder=GeminiQueryEmbedder(client_scope_factory=activate_gemini_client),
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
        reviewer=EvidenceReviewService(
            agent=EVIDENCE_REVIEWER_AGENT,
            runtime_scope_factory=activate_gemini_agent_runtime,
        ),
        direct_answerer=DirectAnswerService(
            agent=DIRECT_ANSWER_AGENT,
            runtime_scope_factory=activate_gemini_agent_runtime,
            repository=repository,
            schedule_deadline_check=schedule_deadline_check,
            delta_reporter=delta_reporter,
            progress=progress,
        ),
        evidence_answerer=EvidenceAnswerService(
            agent=EVIDENCE_ANSWER_AGENT,
            runtime_scope_factory=activate_gemini_agent_runtime,
            repository=repository,
            schedule_deadline_check=schedule_deadline_check,
            delta_reporter=delta_reporter,
            progress=progress,
        ),
        organizer=ResearchHandoffService(
            agent=RESEARCH_HANDOFF_AGENT,
            runtime_scope_factory=activate_gemini_agent_runtime,
        ),
    )


def build_answering_runner(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    repository: AnswerGenerationRepository,
    schedule_deadline_check: Callable[[UUID, datetime], None],
    progress: AnswerProgressReporter | None = None,
    events: AnswerEventReporter | None = None,
    delta_reporter: AnswerDeltaReporter | None = None,
) -> AnsweringRunner:
    return AnsweringRunner(
        phases_factory=lambda: _build_answering_phases(
            session_factory=session_factory,
            repository=repository,
            schedule_deadline_check=schedule_deadline_check,
            events=events,
            delta_reporter=delta_reporter,
            progress=progress,
        ),
        progress=progress,
        events=events,
    )


@asynccontextmanager
async def activate_external_search() -> AsyncIterator[ExternalSearch]:
    from app.agent.evidence_collection.external_search.agentcore import (
        AgentCoreWebSearchGateway,
    )
    from app.agent.evidence_collection.external_search.agentcore_spec import (
        AGENTCORE_WEB_SEARCH_SPEC,
    )
    from app.agent.evidence_collection.external_search.service import (
        ExternalSearchService,
    )

    async with activate_gemini_agent_runtime() as query_runtime:
        # gateway は自 AWS アカウントの resource なので内部宛 client を使う。
        # 外部宛 factory は egress proxy を強制注入するため、署名済みリクエストが
        # proxy へ迂回して失敗する。
        async with make_internal_async_client(
            timeout=AGENTCORE_WEB_SEARCH_SPEC.request_timeout_seconds
        ) as search_client:
            yield ExternalSearchService(
                query_runtime=query_runtime,
                search_gateway=AgentCoreWebSearchGateway(
                    gateway_url=settings.agentcore_gateway_url or "",
                    region=settings.aws_region or "",
                    client=search_client,
                ),
            )
