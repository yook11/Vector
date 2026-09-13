"""CurationのAI判定と、結果・監査・Outboxの原子的な保存を担う。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai_providers.errors import AIProviderError
from app.analysis.curation.ai.base import BaseCurator
from app.analysis.curation.ai.envelope import CurationCall
from app.analysis.curation.domain import Noise, Signal
from app.analysis.curation.domain.ready import ReadyForCuration
from app.analysis.curation.errors import to_curation_error
from app.analysis.curation.events import ArticleCuratedSignal
from app.analysis.curation.metrics import record_curation_processing_outcome
from app.analysis.curation.repository import CurationRepository
from app.audit.stages.curation import CurationAuditRepository
from app.logfire.article_stage import set_curation_stage_result
from app.models.outbox_event import OutboxEvent

logger = structlog.get_logger(__name__)

# outcome_code (pipeline_events) — stage 'curation' と語彙整合 (assessed_* と対称)。
_CURATED_SIGNAL_CODE = "curated_signal"
_CURATED_NOISE_CODE = "curated_noise"


class CurationCompletionKind(StrEnum):
    """保存または処理済み確認による正常終了の種類。"""

    SIGNAL = "signal"
    NOISE = "noise"
    ALREADY_CURATED = "already_curated"


@dataclass(frozen=True, slots=True)
class CurationCompletion:
    """Signalの新規保存時だけ保存済みcuration_idを伴う正常終了。"""

    kind: CurationCompletionKind
    curation_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CurationCompletionKind):
            raise TypeError("kind must be CurationCompletionKind")
        if self.kind is CurationCompletionKind.SIGNAL:
            if type(self.curation_id) is not int or self.curation_id <= 0:
                raise ValueError("signal requires a positive integer curation ID")
        elif self.curation_id is not None:
            raise ValueError("only signal may carry a curation ID")


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
    ) -> CurationCompletion:
        """結果のcommitまたは重複保存の見送りを正常終了として返す。"""
        try:
            envelope = await curator.curate(
                title=ready.original_title,
                content=ready.original_content,
            )
        except AIProviderError as exc:
            raise to_curation_error(exc) from exc

        async with self._session_factory() as session:
            match envelope:
                case CurationCall(result=Signal()):
                    curation_id = await CurationRepository(session).save_signal(
                        envelope, analyzable_article_id=ready.analyzable_article_id
                    )
                    if curation_id is None:
                        # race lost — 勝者 task が audit を焼く
                        logger.info(
                            "curate_race_loss_signal",
                            analyzable_article_id=ready.analyzable_article_id,
                        )
                        set_curation_stage_result("skipped")
                        return CurationCompletion(
                            CurationCompletionKind.ALREADY_CURATED
                        )
                    await CurationAuditRepository(session).append_signal(
                        ready=ready,
                        envelope=envelope,
                        code=_CURATED_SIGNAL_CODE,
                    )
                    event = ArticleCuratedSignal(
                        analyzable_article_id=ready.analyzable_article_id,
                        curation_id=curation_id,
                    )
                    session.add(
                        OutboxEvent(
                            event_type=ArticleCuratedSignal.EVENT_TYPE,
                            schema_version=ArticleCuratedSignal.SCHEMA_VERSION,
                            payload=event.model_dump(mode="json"),
                        )
                    )
                    await session.commit()
                    logger.info(
                        "curation_completed",
                        analyzable_article_id=ready.analyzable_article_id,
                        curation_id=curation_id,
                    )
                    set_curation_stage_result("signal")
                    record_curation_processing_outcome("signal")
                    return CurationCompletion(
                        CurationCompletionKind.SIGNAL, curation_id
                    )

                case CurationCall(result=Noise()):
                    noise_id = await CurationRepository(session).save_noise(
                        envelope, analyzable_article_id=ready.analyzable_article_id
                    )
                    if noise_id is None:
                        # race lost — 勝者 task が audit を焼く
                        logger.info(
                            "curate_race_loss_noise",
                            analyzable_article_id=ready.analyzable_article_id,
                        )
                        set_curation_stage_result("skipped")
                        return CurationCompletion(
                            CurationCompletionKind.ALREADY_CURATED
                        )
                    await CurationAuditRepository(session).append_noise(
                        ready=ready,
                        envelope=envelope,
                        code=_CURATED_NOISE_CODE,
                    )
                    await session.commit()
                    logger.info(
                        "curate_persisted_noise",
                        analyzable_article_id=ready.analyzable_article_id,
                        noise_id=noise_id,
                    )
                    set_curation_stage_result("noise")
                    record_curation_processing_outcome("noise")
                    return CurationCompletion(CurationCompletionKind.NOISE)

                case _:
                    assert_never(envelope)
