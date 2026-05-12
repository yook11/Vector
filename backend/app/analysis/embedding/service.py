"""EmbeddingService — Stage 5 のユースケース組み立てと永続化境界。

案 3 (厚い Ready + 下流 Stage 自身が処理開始時に構築) に従い、precondition
チェック (analysis 存在 + 既存 embedding 不在) と embedder 入力 text の取得は
``ReadyForEmbedding`` が構造保証する。本 Service は execute 内で
DB fetch / None チェックを行わず、``ready.text_for_embedding`` を直接 embedder に
渡す。

Stage 5 は pipeline 終端ゆえ Outcome / Entity の伝搬価値が無いため、execute は
副作用 (永続化) のみを行い ``None`` を返す (Stage 4 Assessment 同型)。楽観ロック
採用上不可避な「並行 update で先に書き込まれていたため自分の save が空振り
する」状況は業務正常パスとして log + 短絡で抜ける (読戻しは行わない、勝者 task
が自身の audit を焼くため敗者経路で audit / commit は呼ばない — actor SSoT)。

AI 呼び出しは session 外で行う (slow IO 中の DB 接続専有を排除 — Phase 1 で
確立した pattern)。``embedder.embed_document`` は永続化可能性を型レベルで
保証する ``EmbeddingVector`` を返し、VO 構造違反は AI 境界内で Layer 2-B
(``EmbeddingResponseInvalidError``) に詰め替え済 (BC 境界原則:
feedback_bc_boundary_guarantees_downstream)。本 Service の ACL 責務は
``AIProviderError`` を Stage 5 marker (``EmbeddingRecoverableError`` /
``EmbeddingTerminalSkipError``) に詰め替えるところのみ。Task 層は Stage 5
marker で 2 marker dispatch + catch-all を行う。
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.embedding.ai.base import BaseEmbedder
from app.analysis.embedding.audit_repository import EmbeddingAuditRepository
from app.analysis.embedding.domain.ready import ReadyForEmbedding
from app.analysis.embedding.errors import to_embedding_error
from app.analysis.embedding.repository import EmbeddingRepository
from app.analysis.errors.provider import AIProviderError

logger = structlog.get_logger(__name__)


class EmbeddingService:
    """1 analysis の埋め込み生成と永続化を行うアトミックなユースケース。

    セッションの管理はサービス内部で完結し、呼び出し側は session factory のみ
    渡す (feedback_session_factory_di)。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(self, ready: ReadyForEmbedding, embedder: BaseEmbedder) -> None:
        """Ready 型を入力に埋め込みベクトルを生成し永続化する。

        案 3: 受け取った Ready は precondition (analysis 存在 + embedding 未生成)
        + embedder 入力 text の全揃え + audit 用 ``article_id`` を構造保証している。
        Service は値 fetch / None チェックを持たず、``ready.text_for_embedding`` を
        直接 embedder に渡す。

        順序 (Stage 4 actor SSoT 思想):
        1. embedder 呼び出し (slow IO、session 外) — 戻り値は永続化可能性を
           型レベルで保証する ``EmbeddingVector`` (Layer 2-B VO 違反は embedder
           内で ``EmbeddingResponseInvalidError`` に詰め替えて raise 済)
        2. session を開いて条件付き UPDATE で永続化
        3. 保存成功 (rowcount=1) なら audit を焼いて同 tx で commit
        4. 並行 update に先を越された (rowcount=0) 場合は audit / commit せず短絡
           (勝者 task が自身の audit を焼く、二重記録回避)

        Raises:
            ``EmbeddingRecoverableError`` / ``EmbeddingTerminalSkipError``
            (Task 層 2 marker dispatch に委ねる)。``AIProviderError`` は ACL で
            Stage 5 marker に詰め替えてから raise される。
        """
        try:
            vector = await embedder.embed_document(ready.text_for_embedding)
        except AIProviderError as exc:
            # ACL boundary: provider error を Stage 5 Layer 1 marker に wrap。
            # ``from exc`` で __cause__ に元 AIProvider*Error を紐付け、
            # ``recording.py::_extract_error_chain`` が wrapper marker → 元
            # provider error の 2 段以上を audit ``payload.error_chain`` に
            # 記録できるようにする。
            raise to_embedding_error(exc) from exc

        async with self._session_factory() as session:
            saved = await EmbeddingRepository(session).save(
                vector,
                analysis_id=ready.analysis_id,
                model_name=embedder.MODEL,
            )
            if not saved:
                # 楽観ロックにより並行 update で先に書き込まれていた → 業務正常パス。
                # audit / commit は呼ばない (勝者 task が自身の audit を焼く、
                # actor SSoT 維持、二重記録回避)。
                logger.info(
                    "embedding_concurrent_write",
                    analysis_id=ready.analysis_id,
                )
                return
            # 保存成功 — 業務 UPDATE + audit を同一 tx で commit
            await EmbeddingAuditRepository(session).append_success(
                ready=ready,
                embedder=embedder,
            )
            await session.commit()

        logger.info(
            "embedding_completed",
            analysis_id=ready.analysis_id,
            model=embedder.MODEL,
        )
