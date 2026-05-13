"""Stage 5 (Embedding) taskiq タスク。

パイプライン終端 — assess_content (Stage 4) から ``EmbeddingTrigger`` 経由で
chain される。

設計方針 (2026-05-12 確定、案 3): Stage 5 Task 自身が処理開始時に DB から
``ReadyForEmbedding`` を構築する。上流から受領するのは ``EmbeddingTrigger``
(analysis_id のみ) であり、precondition 検証 + embedder 入力 text + audit 用
``article_id`` の取得は本 Task 内で ``Ready.try_advance_from`` を呼んで完結させる。

処理順序ポリシー:
1. DB から Ready 構築 (precondition 検証)
2. Ready が None なら早期 skip (stale trigger / 既 embedded を log 記録)
3. AI を呼ぶ見込みが立った後で rate limit acquire
4. Service.execute で AI 呼び出し + 永続化

rate limit を Ready 構築より前に取得すると、precondition 未充足の stale trigger
でも AI quota / Redis rate limit を消費してしまう。案 3 では「DB から処理可能性
を確認してから quota を消費する」順序が正解。

失敗 dispatch / audit は ``EmbeddingFailureHandler`` (``failure_handling.py``)
に委譲する (Stage 3 / Stage 4 と同型)。Task 層は marker の意味を持たず、Handler
の戻り値 (``reraise: bool``) だけを解釈して taskiq の raise / return semantics
に変換する。
"""

from __future__ import annotations

import structlog
from taskiq import Context, TaskiqDepends

from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.domain.ready import EmbeddingTrigger, ReadyForEmbedding
from app.analysis.embedding.failure_handling import EmbeddingFailureHandler
from app.analysis.embedding.repository import EmbeddingRepository
from app.analysis.embedding.service import EmbeddingService
from app.brokers import broker_embedding, is_last_attempt

logger = structlog.get_logger(__name__)


@broker_embedding.task(
    task_name="generate_embedding",
    timeout=60,
    max_retries=2,
    retry_on_error=True,
)
async def generate_embedding(
    trigger: EmbeddingTrigger,
    ctx: Context = TaskiqDepends(),
) -> None:
    """単一 analysis に対してベクトル埋め込みを生成する (Stage 5)。

    案 3: 上流から受領する ``EmbeddingTrigger`` は analysis_id のみ運び
    precondition を保証しない。本 Task が処理開始時に Ready を構築し、
    precondition 充足を確認してから rate limit acquire + Service 呼び出しに進む。
    """
    session_factory = ctx.state.session_factory
    embedder: BaseEmbedder = ctx.state.embedder

    # Stage 5 自身が DB から処理可能性を検証 (案 3: 処理開始時に Ready 構築)
    async with session_factory() as session:
        ready = await ReadyForEmbedding.try_advance_from(
            analysis_id=trigger.analysis_id,
            embedding_repo=EmbeddingRepository(session),
        )
    if ready is None:
        logger.info(
            "generate_embedding_skipped",
            analysis_id=trigger.analysis_id,
            reason="precondition_not_met",
        )
        return

    # AI を呼ぶ見込みが立ってから rate limit acquire (stale trigger で quota を
    # 消費しない設計)
    gate = ctx.state.provider_rate_limit_gate
    if not await gate.acquire(embedder.rate_policy):
        logger.warning(
            "generate_embedding_daily_quota",
            analysis_id=ready.analysis_id,
        )
        return

    svc = EmbeddingService(session_factory)
    handler = EmbeddingFailureHandler(session_factory)
    attempt = int(ctx.message.labels.get("retry_count", 0)) + 1

    try:
        await svc.execute(ready, embedder)
    except Exception as exc:
        reraise = await handler.handle(
            ready=ready,
            exc=exc,
            attempt=attempt,
            last_attempt=is_last_attempt(ctx),
        )
        if reraise:
            raise
        return

    # Stage 5 はパイプライン終端、chain firing なし。
