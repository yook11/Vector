"""Stage 3 Curation の application service。

``ReadyForCuration`` を入力に AI 抽出を実行し、結果を Signal/Noise に振り分けて
業務行と成功 audit を同一 transaction で永続化する。

AI 呼び出しは session 外で行い、並行実行に負けた場合は audit / commit せず
``None`` を返す。失敗は catch せず typed exception のまま呼び出し元へ伝搬し、
失敗時の retry / audit / DELETE 方針は ``CurationFailureHandler`` に委ねる。

Returns:
    Signal 保存成功時は ``article_curations.id``。
    Noise 保存成功時または race 敗北時は ``None``。
"""

from __future__ import annotations

from typing import assert_never

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.ai_provider_errors import AIProviderError
from app.analysis.curation.ai.base import BaseCurator
from app.analysis.curation.ai.envelope import CurationCall
from app.analysis.curation.audit import build_curation_audit_input
from app.analysis.curation.domain import Noise, Signal
from app.analysis.curation.domain.ready import ReadyForCuration
from app.analysis.curation.errors import map_provider_to_curation
from app.analysis.curation.repository import CurationRepository
from app.audit.stages.curation import CurationAuditRepository

logger = structlog.get_logger(__name__)

# outcome_code (pipeline_events) — stage 'curation' と語彙整合 (assessed_* と対称)。
_CURATED_SIGNAL_CODE = "curated_signal"
_CURATED_NOISE_CODE = "curated_noise"


class CurationService:
    """1 記事の curation (relevance 判定 + 翻訳要約) を行うアトミックなユースケース。

    Stage 3: 原文を読み、翻訳タイトル・事実ベース要約を抽出して
    signal / noise に振り分ける。分類(カテゴリ・トピック・インパクト)は
    Stage 4 の責務。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(
        self,
        ready: ReadyForCuration,
        curator: BaseCurator,
    ) -> int | None:
        """1 記事に対して curation を実行する。

        precondition は ``ReadyForCuration`` で構造保証済。失敗は Stage 3
        Layer 1 marker (``CurationRecoverableError`` /
        ``CurationTerminalKeepError`` / ``CurationTerminalDropError``) と
        ``CurationResponseInvalidError`` で raise する (Task 層が dispatch)。
        provider 由来例外は本 boundary で ``map_provider_to_curation`` により
        Stage 3 marker に詰め替える (Anti-Corruption Layer)。

        Returns:
            signal 勝者の ``article_curations.id``、noise 勝者と race 敗北は
            ``None`` (Task 層は ``None`` で Stage 4 chain を抑止)。
        """
        # AI 呼び出しは session 外。provider error は Stage 3 marker に詰め替えて
        # からそのまま伝搬する (ACL boundary)。Stage 3 specific (Layer 2-B) は
        # curator 内で既に Stage 3 marker subclass として raise されるため
        # 詰め替え不要。
        try:
            envelope = await curator.curate(
                title=ready.original_title,
                content=ready.original_content,
            )
        except AIProviderError as exc:
            raise map_provider_to_curation(exc) from exc

        audit_input = build_curation_audit_input(
            original_content=ready.original_content
        )
        async with self._session_factory() as session:
            match envelope:
                case CurationCall(result=Signal()):
                    curation_id = await CurationRepository(session).save_signal(
                        envelope, article_id=ready.article_id
                    )
                    if curation_id is None:
                        # race lost — 勝者 task が audit を焼く
                        logger.info(
                            "curate_race_loss_signal",
                            article_id=ready.article_id,
                        )
                        return None
                    await CurationAuditRepository(session).append_signal(
                        ready=ready,
                        envelope=envelope,
                        code=_CURATED_SIGNAL_CODE,
                        **audit_input,
                    )
                    await session.commit()
                    logger.info(
                        "curation_completed",
                        article_id=ready.article_id,
                        curation_id=curation_id,
                    )
                    return curation_id

                case CurationCall(result=Noise()):
                    noise_id = await CurationRepository(session).save_noise(
                        envelope, article_id=ready.article_id
                    )
                    if noise_id is None:
                        # race lost — 勝者 task が audit を焼く
                        logger.info(
                            "curate_race_loss_noise",
                            article_id=ready.article_id,
                        )
                        return None
                    await CurationAuditRepository(session).append_noise(
                        ready=ready,
                        envelope=envelope,
                        code=_CURATED_NOISE_CODE,
                        **audit_input,
                    )
                    await session.commit()
                    logger.info(
                        "curate_persisted_noise",
                        article_id=ready.article_id,
                        noise_id=noise_id,
                    )
                    # noise 勝者でも Stage 4 chain しないため None を返す
                    return None

                case _:
                    assert_never(envelope)
