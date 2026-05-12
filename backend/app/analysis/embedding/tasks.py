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

エラー dispatch (Stage 4 と完全同形):
- ``EmbeddingTerminalSkipError``: audit 焼いて即 return (no retry)
- ``EmbeddingRecoverableError``: audit 焼いて is_last_attempt 判定で
  exhaust なら return / 否則 ``raise`` (taskiq 再試行)
- catch-all (想定外): audit 焼いて exhaust なら return / 否則 ``raise``
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from taskiq import Context, TaskiqDepends

from app.analysis._limiter_factory import _build_limiters
from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.audit_repository import EmbeddingAuditRepository
from app.analysis.embedding.domain.ready import EmbeddingTrigger, ReadyForEmbedding
from app.analysis.embedding.errors import (
    EmbeddingRecoverableError,
    EmbeddingTerminalSkipError,
)
from app.analysis.embedding.repository import EmbeddingRepository
from app.analysis.embedding.service import EmbeddingService
from app.analysis.rate_limiter import (
    RateLimitExceededError as _RateLimitExceededError,
)
from app.brokers import broker_embedding, is_last_attempt
from app.observability.redact import redact_secrets

logger = structlog.get_logger(__name__)


async def _record_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    ready: ReadyForEmbedding,
    exc: BaseException,
    attempt: int,
) -> None:
    """Stage 5 失敗 1 件を記録する (caller 観点: business tx と独立に焼ける)。

    実装は別 session / 別 tx を ``session_factory`` で開き
    ``EmbeddingAuditRepository.append_failure`` を 1 行 append + commit する。
    Repository は class API のみで tx 境界を握らないため、別 session 開閉と
    commit は本 helper (Task 層) の責務 (Stage 4 と同 pattern)。

    audit 書込みは best-effort: DB 落ち / migration 漏れ / schema 不整合などで
    INSERT または commit が失敗しても、業務 task を殺さないよう例外を呑んで
    ``embedding_failure_audit_dropped`` 構造ログにフォールバックする
    (運用シグナル、監査の audit ではない)。SDK exception message に key prefix
    / Authorization header が混入しうるため、DB payload と同 pattern で
    ログ経路にも ``redact_secrets`` を通す (red-team chain γ-2 対称化)。
    """
    try:
        async with session_factory() as session:
            await EmbeddingAuditRepository(session).append_failure(
                ready=ready, exc=exc, attempt=attempt
            )
            await session.commit()
    except Exception as audit_exc:
        logger.exception(
            "embedding_failure_audit_dropped",
            analysis_id=ready.analysis_id,
            attempt=attempt,
            business_error_class=f"{type(exc).__module__}.{type(exc).__qualname__}",
            business_error_message=redact_secrets(str(exc))[:500],
            audit_error_class=(
                f"{type(audit_exc).__module__}.{type(audit_exc).__qualname__}"
            ),
            audit_error_message=redact_secrets(str(audit_exc))[:500],
        )


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
    rpm_limiter, rpd_limiter = _build_limiters(
        "embed", embedder.MODEL, embedder.RPM, embedder.RPD
    )
    try:
        if rpd_limiter is not None:
            await rpd_limiter.acquire()
        if rpm_limiter is not None:
            await rpm_limiter.acquire()
    except _RateLimitExceededError:
        logger.warning(
            "generate_embedding_daily_quota",
            analysis_id=ready.analysis_id,
        )
        return

    # Service 呼び出し（session は内部で管理、戻り値なし — log は Service 内で完結）
    svc = EmbeddingService(session_factory)
    attempt = int(ctx.message.labels.get("retry_count", 0)) + 1
    try:
        await svc.execute(ready, embedder)
    except EmbeddingTerminalSkipError as exc:
        # Layer 1 marker: 永続的失敗 → audit 焼いて即 return
        # (taskiq retry なし、analysis 保持)。
        await _record_failure(session_factory, ready=ready, exc=exc, attempt=attempt)
        logger.warning(
            "generate_embedding_terminal_skip",
            analysis_id=ready.analysis_id,
            code=getattr(exc, "code", None),
        )
        return
    except EmbeddingRecoverableError as exc:
        # Layer 1 marker (Layer 2-B EmbeddingResponseInvalidError も継承で拾う):
        # 一時的失敗 → audit 焼いて is_last_attempt でトリアージ。
        await _record_failure(session_factory, ready=ready, exc=exc, attempt=attempt)
        if is_last_attempt(ctx):
            logger.warning(
                "generate_embedding_recoverable_exhausted",
                analysis_id=ready.analysis_id,
                code=getattr(exc, "code", None),
            )
            return
        raise  # taskiq 再試行
    except Exception as exc:
        # catch-all (想定外): audit 焼いて exhausted なら return、否則 raise。
        await _record_failure(session_factory, ready=ready, exc=exc, attempt=attempt)
        if is_last_attempt(ctx):
            logger.exception(
                "generate_embedding_unexpected_exhausted",
                analysis_id=ready.analysis_id,
            )
            return
        raise
