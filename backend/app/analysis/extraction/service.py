"""Extraction サービス — Stage 3 の処理組み立てと DB 永続化。

Pattern A' (spec §3.2 / §6.1 / §7) で ``ReadyForExtraction`` を Stage 間 passport
として受け取り、precondition (Article 存在 + Extraction/Noise 未生成 + 本文
サイズ ≤ hard cap) は型レベルで構造保証されている。本サービスは:

- AI 呼び出し (session 外、slow IO 中の DB 接続専有を排除 — spec §4.7)
- ``relevance`` で signal/noise を振り分け、それぞれ別テーブルへ永続化
- race 敗北時 (Repository が ``None`` 返却) は audit / commit を焼かず短絡
  (勝者 SSoT、Stage 4 AssessmentService / Stage 5 EmbeddingService と同型)
- 各成功経路で同 tx に audit を焼付 (``ExtractionAuditRepository`` の
  ``append_extracted`` / ``append_noise`` を呼ぶだけ、shape は repository SSoT、
  Stage 4 AssessmentService と同じく ``execute`` 内インライン match で分岐)
- 内容起因 Permanent failure 経路で 1 tx 内 audit + article DELETE
  (``mark_article_unprocessable``、audit は ``append_drop_article`` に委譲)

の責務に縮退している。Service は ``PipelineEventRepository.append`` を直接
呼ばない / ``ExtractionPayload`` を組み立てない (audit_repository が SSoT)。

戻り値は ``int | None`` 一本化 (Stage 4 AssessmentService と完全対称):
- signal 勝者: 新規 ``article_extractions.id`` を返し Task 層で
  ``assess_content.kiq(AssessmentTrigger(extraction_id=...))`` を発火
- noise 勝者: ``None`` を返す (Stage 4 chain しない、業務行は焼く / audit も焼く)
- race 敗北 (signal / noise 両方): ``None`` を返す (audit / commit せず短絡)

失敗は全て typed exception で **そのまま raise** する (Service は catch しない、
spec §原則 4)。Task 層が Layer 1 marker (``NonRetryableDropArticle`` /
``NonRetryableKeepArticle`` / ``RetryableError``) で dispatch する責務。
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
from app.repositories.articles import ArticleRepository

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
                        entity_count=len(envelope.result.entities),
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
                        entity_count=len(envelope.result.entities),
                    )
                    # noise 勝者でも Stage 4 chain しないため None を返す
                    return None

                case _:
                    assert_never(envelope)

    async def mark_article_unprocessable(
        self,
        article_id: int,
        original_content: str,
        *,
        code: str,
        exc: BaseException,
        extractor: BaseExtractor,
    ) -> None:
        """内容起因 Permanent failure を 1 tx で焼付け + 記事 DELETE する。

        順序は **audit INSERT 先、DELETE 後** — ``source_id`` の自動逆引きが
        Article 存在中にしか動かないため。FK は ``ondelete=SET NULL`` 済で
        DELETE 後も audit 行は残る。``original_content`` は caller が DELETE
        前に読み出して渡す。
        """
        async with self._session_factory() as session:
            await ExtractionAuditRepository(session).append_drop_article(
                article_id=article_id,
                original_content=original_content,
                code=code,
                exc=exc,
                extractor=extractor,
            )
            deleted = await ArticleRepository(session).delete_by_id(article_id)
            await session.commit()

            logger.warning(
                "extraction_article_unprocessable",
                article_id=article_id,
                code=code,
                deleted_rows=deleted,
                error_class=f"{type(exc).__module__}.{type(exc).__qualname__}",
            )
