"""Agentの外部応答、実DB操作の確定と結果の観測を支える。"""

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.agent.evidence_collection.external_search.contract import (
    ExternalSearchHit,
    ExternalSearchProviderError,
)
from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalQueryEmbedding,
)
from app.agent.running.answer_generation import (
    AgentAnswerGenerationRepository,
    AnswerGenerationStarted,
)
from app.agent.running.attempt_start import (
    AgentRunAttemptStartRepository,
    StartRunFailure,
)
from app.agent.running.creation import AgentRunCreationRepository
from app.ai_providers.errors import AIProviderError
from app.analysis.embedding.domain.value_objects import EMBEDDING_DIMENSION
from app.models.agent_message import AgentMessage, AgentMessageSource


class AgentProviderResponses:
    """業務処理を通したまま、AIと外部検索の応答だけを固定する。"""

    def __init__(self):
        self.answer: str | list[str] = ""
        self.search_hits: list[ExternalSearchHit] = []
        self.planning_response: dict | AIProviderError = {
            "plan_type": "search",
            "research_tasks": [
                {
                    "research_goal": "売上動向を確認する",
                    "article_search_queries": ["売上動向"],
                }
            ],
        }
        self.evidence_review_response = {
            "selections": [
                {
                    "option_index": 0,
                    "claim": "売上は前年同期比10%増",
                    "why_selected": "売上動向の根拠になるため",
                }
            ],
            "missing": ["利益率は未確認"],
        }
        self.handoff_response: dict | AIProviderError = {
            "collected_overview": "売上動向を確認した",
            "unresolved_points": "利益率は未確認",
            "next_search_guidance": "利益率を調べる",
        }
        self.embedding_error: AIProviderError | None = None
        self.search_error: ExternalSearchProviderError | None = None
        self.calls: list[str] = []
        self.answer_attempts: list[int] = []
        self.embedding_requests = []
        self.search_requests = []

    async def call(self, agent, input, *, attempt_number):
        self.calls.append(agent.name)
        outputs = {
            "question_planner": self.planning_response,
            "external_query_generator": {"queries": ["売上動向"]},
            "evidence_reviewer": self.evidence_review_response,
            "research_handoff": self.handoff_response,
        }
        response = outputs[agent.name]
        if isinstance(response, AIProviderError):
            raise response
        return agent.output_type.model_validate(response)

    async def stream_text(self, agent, input, *, attempt_number):
        self.calls.append(agent.name)
        self.answer_attempts.append(attempt_number)
        if isinstance(self.answer, list):
            yield self.answer[attempt_number - 1]
        else:
            yield self.answer

    async def search(self, request):
        self.search_requests.append(request)
        if self.search_error is not None:
            raise self.search_error
        return list(self.search_hits)

    async def embed_queries(self, queries):
        self.embedding_requests.append(queries)
        if self.embedding_error is not None:
            raise self.embedding_error
        return [
            InternalQueryEmbedding(
                query=query,
                vector=(1.0,) + (0.0,) * (EMBEDDING_DIMENSION - 1),
            )
            for query in queries.queries
        ]


@dataclass(frozen=True)
class PreparedAnsweringRun:
    run_id: UUID
    thread_id: UUID
    attempt_epoch: int
    answer_started_at: datetime


async def create_user_run(session_factory, *, user_id, question):
    async with session_factory() as session:
        async with session.begin():
            created = await AgentRunCreationRepository(session).create_user_run(
                user_id=user_id, question=question, thread_id=None
            )
    return created


async def create_answering_run(session_factory, *, user_id, question):
    async with session_factory() as session:
        async with session.begin():
            created = await AgentRunCreationRepository(session).create_user_run(
                user_id=user_id, question=question, thread_id=None
            )

    async with session_factory() as session:
        async with session.begin():
            attempt_epoch = await AgentRunAttemptStartRepository(session).start_run(
                created.run_id
            )
    if isinstance(attempt_epoch, StartRunFailure):
        raise RuntimeError(
            f"テスト用ランの実行を開始できませんでした: {attempt_epoch.reason}"
        )

    answer_generation = await AgentAnswerGenerationRepository(
        session_factory, created.run_id, attempt_epoch
    ).start_answer_generation()
    if not isinstance(answer_generation, AnswerGenerationStarted):
        raise RuntimeError("テスト用ランの回答生成を開始できませんでした")

    return PreparedAnsweringRun(
        run_id=created.run_id,
        thread_id=created.thread_id,
        attempt_epoch=attempt_epoch,
        answer_started_at=answer_generation.answer_started_at,
    )


@contextmanager
def fail_after_source_write(*, thread_id):
    written_answer_ids = []

    def fail_after_flush(session, _flush_context):
        source_rows = [
            row for row in session.new if isinstance(row, AgentMessageSource)
        ]
        if not source_rows:
            return

        answer_ids = (
            session.connection()
            .execute(
                select(AgentMessage.id).where(
                    AgentMessage.thread_id == thread_id,
                    AgentMessage.role == "assistant",
                    AgentMessage.id.in_([source.message_id for source in source_rows]),
                )
            )
            .scalars()
            .all()
        )
        if not answer_ids:
            return

        written_answer_ids.extend(answer_ids)
        raise RuntimeError("テスト用の回答保存エラー")

    event.listen(Session, "after_flush", fail_after_flush)
    try:
        yield written_answer_ids
    finally:
        event.remove(Session, "after_flush", fail_after_flush)


@dataclass(frozen=True)
class SavedRunResult:
    run: dict
    assistant_messages: list[dict]
    sources: list[dict]


async def fetch_saved_run_result(database, *, run_id, thread_id):
    """別接続から全回答を読み、Runが参照しない余分な回答も比較に含める。"""
    async with database.connect("vector_app") as connection:
        run = await connection.fetchrow(
            "SELECT id, status, assistant_message_id, error_code, attempt_epoch, "
            "answer_started_at "
            "FROM agent_runs WHERE id=$1 AND thread_id=$2",
            run_id,
            thread_id,
        )
        messages = await connection.fetch(
            "SELECT id, seq, content, missing_aspects FROM agent_messages "
            "WHERE thread_id=$1 AND role='assistant' ORDER BY seq",
            thread_id,
        )
        sources = await connection.fetch(
            "SELECT s.id, s.message_id, s.ordinal, s.kind, s.source_ref, "
            "s.analyzed_article_id, s.url, s.title, s.source_name, "
            "s.published_at, s.evidence_claim "
            "FROM agent_message_sources s "
            "JOIN agent_messages m ON m.id=s.message_id "
            "WHERE m.thread_id=$1 ORDER BY m.seq, s.ordinal",
            thread_id,
        )
    decoded_messages = [dict(row) for row in messages]
    for message in decoded_messages:
        message["missing_aspects"] = json.loads(message["missing_aspects"])
    return SavedRunResult(dict(run), decoded_messages, [dict(row) for row in sources])
