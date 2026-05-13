"""Stage 3 Extraction の application service。

``ReadyForExtraction`` を入力に AI 抽出を実行し、結果を Signal/Noise に振り分けて
業務行と成功 audit を同一 transaction で永続化する。

AI 呼び出しは session 外で行い、並行実行に負けた場合は audit / commit せず
``None`` を返す。失敗は catch せず typed exception のまま呼び出し元へ伝搬し、
失敗時の retry / audit / DELETE 方針は ``ExtractionFailureHandler`` に委ねる。

Returns:
    Signal 保存成功時は ``article_extractions.id``。
    Noise 保存成功時または race 敗北時は ``None``。
"""

from __future__ import annotations

from typing import assert_never

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.extraction.ai.base import BaseExtractor
from app.analysis.extraction.ai.envelope import ExtractionCall
from app.analysis.extraction.audit_repository import ExtractionAuditRepository
from app.analysis.extraction.domain import Noise, Signal
from app.analysis.extraction.domain.ready import ReadyForExtraction
from app.analysis.extraction.repository import ExtractionRepository

logger = structlog.get_logger(__name__)

_EXTRACTED_CODE = "extracted"
_EXTRACTED_AS_NOISE_CODE = "extracted_as_noise"


class ExtractionService:
    """1 記事の事実抽出と結果永続化を行うアトミックなユースケース。

    Stage 3: 原文を読み、翻訳タイトル・事実ベース要約・エンティティを抽出する。
    分類(カテゴリ・トピック・インパクト)は Stage 4 の責務。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(
        self,
        ready: ReadyForExtraction,
        extractor: BaseExtractor,
    ) -> int | None:
        """1 記事に対して事実抽出を実行する。

        precondition は ``ReadyForExtraction`` で構造保証済。失敗は Layer 2
        例外でそのまま raise する (Task 層が Layer 1 marker で dispatch)。

        Returns:
            signal 勝者の ``article_extractions.id``、noise 勝者と race 敗北は
            ``None`` (Task 層は ``None`` で Stage 4 chain を抑止)。
        """
        # AI 呼び出しは session 外 (例外はそのまま伝搬、catch しない)
        envelope = await extractor.extract(
            title=ready.original_title,
            content=ready.original_content,
        )

        async with self._session_factory() as session:
            match envelope:
                case ExtractionCall(result=Signal()):
                    extraction_id = await ExtractionRepository(session).save_signal(
                        envelope, article_id=ready.article_id
                    )
                    if extraction_id is None:
                        # race lost — 勝者 task が audit を焼く
                        logger.info(
                            "extract_race_loss_signal",
                            article_id=ready.article_id,
                        )
                        return None
                    await ExtractionAuditRepository(session).append_extracted(
                        ready=ready,
                        envelope=envelope,
                        code=_EXTRACTED_CODE,
                    )
                    await session.commit()
                    logger.info(
                        "extraction_completed",
                        article_id=ready.article_id,
                        extraction_id=extraction_id,
                    )
                    return extraction_id

                case ExtractionCall(result=Noise()):
                    noise_id = await ExtractionRepository(session).save_noise(
                        envelope, article_id=ready.article_id
                    )
                    if noise_id is None:
                        # race lost — 勝者 task が audit を焼く
                        logger.info(
                            "extract_race_loss_noise",
                            article_id=ready.article_id,
                        )
                        return None
                    await ExtractionAuditRepository(session).append_noise(
                        ready=ready,
                        envelope=envelope,
                        code=_EXTRACTED_AS_NOISE_CODE,
                    )
                    await session.commit()
                    logger.info(
                        "extract_persisted_noise",
                        article_id=ready.article_id,
                        noise_id=noise_id,
                    )
                    # noise 勝者でも Stage 4 chain しないため None を返す
                    return None

                case _:
                    assert_never(envelope)
