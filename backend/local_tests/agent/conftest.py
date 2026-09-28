"""製品のAgentをvector_agentで動かし、準備と観測は所有者の接続で行う。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.agent import composition
from app.agent.evidence_collection import EvidenceCollectionService
from app.agent.evidence_collection.external_search.contract import ExternalSearchHit
from app.agent.evidence_collection.external_search.service import ExternalSearchService
from app.agent.evidence_collection.internal_search.ai import gemini
from app.agent.evidence_review import EvidenceReviewService
from app.agent.running.deadline.scheduling import AgentDeadlineScheduler
from app.db.engine import create_worker_engine
from app.db.session import caller_managed_session_factory
from local_tests.agent.deadline_worker_support import WorkerDeadlineRecovery
from local_tests.agent.support import AgentProviderResponses
from local_tests.agent.worker_support import start_agent_workers


@pytest.fixture
async def agent_workers(system_database, tmp_path):
    async with start_agent_workers(system_database, tmp_path) as workers:
        yield workers


@pytest.fixture
def worker_deadline_recovery(agent_workers):
    return WorkerDeadlineRecovery(agent_workers)


@pytest.fixture
def collect_evidence():
    with patch.object(
        EvidenceCollectionService,
        "collect",
        autospec=True,
        side_effect=EvidenceCollectionService.collect,
    ) as collect:
        yield collect


@pytest.fixture
def review_evidence():
    with patch.object(
        EvidenceReviewService,
        "review",
        autospec=True,
        side_effect=EvidenceReviewService.review,
    ) as review:
        yield review


@pytest.fixture
async def agent_user_id(system_database):
    user_id = uuid4()
    async with system_database.connect("vector") as connection:
        await connection.execute(
            'INSERT INTO auth."user" '
            '(id, name, email, "emailVerified", "createdAt", "updatedAt", role) '
            "VALUES ($1, 'Agent test', 'agent@example.test', "
            "false, now(), now(), 'user')",
            user_id,
        )
    return user_id


@pytest.fixture
def agent_provider_responses(monkeypatch):
    responses = AgentProviderResponses()
    responses.search_hits = [
        ExternalSearchHit(
            url="https://example.com/initial-report",
            title="初回の売上報告",
            content="売上は前年同期比10%増",
            source_name="Example",
        )
    ]

    @asynccontextmanager
    async def runtime_scope():
        yield responses

    @asynccontextmanager
    async def search_scope():
        yield ExternalSearchService(query_runtime=responses, search_gateway=responses)

    monkeypatch.setattr(composition, "activate_gemini_agent_runtime", runtime_scope)
    monkeypatch.setattr(
        composition, "activate_evidence_reviewer_runtime", runtime_scope
    )
    monkeypatch.setattr(composition, "activate_external_search", search_scope)
    monkeypatch.setattr(gemini, "GeminiQueryEmbedder", lambda: responses)
    monkeypatch.setattr(
        composition.settings, "deepseek_api_key", SecretStr("local-test")
    )
    monkeypatch.setattr(
        composition.settings, "agentcore_gateway_url", "https://agent.example.test"
    )
    return responses


@pytest.fixture
async def owner_session_factory(system_database):
    """APIが受け付けるrunの作成を、製品のロールと分けて所有者の接続で行う。"""
    engine = create_async_engine(system_database.url("vector", sqlalchemy=True))
    try:
        yield caller_managed_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
async def agent_context(system_database):
    settings = SimpleNamespace(
        database_url=system_database.url("vector_agent", sqlalchemy=True),
        db_iam_auth=False,
        aws_region=None,
    )
    engine = create_worker_engine(settings, "agent")
    async with engine.connect() as connection:
        role = await connection.scalar(text("SELECT current_user"))
    if role != "vector_agent":
        await engine.dispose()
        raise RuntimeError(f"Agent試験がvector_agent以外で接続している: {role}")
    redis = Mock()
    redis.pipeline.return_value.execute = AsyncMock(return_value=["1-0", True])
    try:
        yield SimpleNamespace(
            state=SimpleNamespace(
                session_factory=caller_managed_session_factory(engine),
                agent_live_redis=redis,
                agent_deadline_scheduler=Mock(spec=AgentDeadlineScheduler),
            )
        )
    finally:
        await engine.dispose()
