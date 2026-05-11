"""Extraction サービス — Stage 3 の処理組み立てと DB 永続化。

Pattern A' (spec §3.2 / §6.1 / §7) で ``ReadyForExtraction`` を Stage 間 passport
として受け取り、precondition (Article 存在 + Extraction/Noise 未生成 + 本文
サイズ ≤ hard cap) は型レベルで構造保証されている。本サービスは:

- AI 呼び出し (session 外、slow IO 中の DB 接続専有を排除 — spec §4.7)
- ``relevance`` で signal/noise を振り分け、それぞれ別テーブルへ永続化
- race 敗北時の読戻し → Outcome 返却
- 各 Outcome 経路で同 tx に audit を焼付 (``ExtractionAuditRepository`` の
  ``append_extracted`` / ``append_noise`` を呼ぶだけ、shape は repository SSoT)
- 内容起因 Permanent failure 経路で 1 tx 内 audit + article DELETE
  (``mark_article_unprocessable``、audit は ``append_drop_article`` に委譲)

の責務に縮退している。Service は ``PipelineEventRepository.append`` を直接
呼ばない / ``ExtractionPayload`` を組み立てない (audit_repository が SSoT)。

失敗は全て typed exception で **そのまま raise** する (Service は catch しない、
spec §原則 4)。Task 層が Layer 1 marker (``NonRetryableDropArticle`` /
``NonRetryableKeepArticle`` / ``RetryableError``) で dispatch する責務。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.extraction.audit_repository import ExtractionAuditRepository
from app.analysis.extraction.domain import Extraction
from app.analysis.extraction.domain.ready import ReadyForExtraction
from app.analysis.extraction.extractor.base import BaseExtractor
from app.analysis.extraction.extractor.envelope import ExtractionCall
from app.analysis.extraction.noise_repository import NoiseRepository
from app.analysis.extraction.repository import ExtractionRepository
from app.observability.categories import SuccessOutcome
from app.repositories.articles import ArticleRepository

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ExtractedOutcome(SuccessOutcome):
    """Stage 3 成功 (signal、新規 INSERT or race 敗北からの読戻し)。

    下流 Stage 4 (assessment) に chain する。
    """

    CODE: ClassVar[str] = "extracted"
    extraction: Extraction


@dataclass(frozen=True, slots=True)
class NoiseOutcome(SuccessOutcome):
    """Stage 3 で noise 判定。``extraction_noises`` に永続化済、chain しない。"""

    CODE: ClassVar[str] = "extracted_as_noise"


# 失敗は全て raise (Outcome union に入れない、spec §原則 4)
ExtractionOutcome = ExtractedOutcome | NoiseOutcome


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
    ) -> ExtractionOutcome:
        """1 記事に対して事実抽出を実行する。

        precondition は ``ReadyForExtraction`` で構造保証済 (Article 存在 +
        Extraction 未生成 + 本文サイズ ≤ hard cap)。本メソッド内で再 fetch
        / None check は行わない。

        失敗は全て Layer 2 例外として **そのまま raise** する (Service は catch
        しない)。Task 層が Layer 1 marker で dispatch する責務 (spec §原則 4)。

        Raises:
            AIProviderError: provider 呼び出し由来の失敗 (Layer 2-A)。
            ExtractionDomainError: Stage 3 工程由来の失敗 (Layer 2-B)。
            Exception: 想定外の例外 (Task 層 catch-all で UNKNOWN ラベル)。
        """
        # AI 呼び出しは session 外 (例外はそのまま伝搬、catch しない)
        envelope = await extractor.extract(
            title=ready.original_title,
            content=ready.original_content,
        )

        if envelope.result.relevance == "noise":
            return await self._persist_noise(ready, envelope, extractor.model_name)
        return await self._persist_signal(ready, envelope, extractor.model_name)

    async def _persist_signal(
        self,
        ready: ReadyForExtraction,
        envelope: ExtractionCall,
        ai_model: str,
    ) -> ExtractedOutcome:
        async with self._session_factory() as session:
            repo = ExtractionRepository(session)
            saved = await repo.save(
                envelope.result,
                article_id=ready.article_id,
                ai_model=ai_model,
            )

            if saved is None:
                # race 敗北 — 勝者を読み戻して合流 (audit は勝者側で焼かれる)
                logger.info(
                    "extraction_concurrent_write",
                    article_id=ready.article_id,
                )
                await session.commit()
                async with self._session_factory() as read_session:
                    saved = await ExtractionRepository(read_session).find_by_article_id(
                        ready.article_id
                    )
                if saved is None:
                    raise RuntimeError(
                        f"extraction_race_winner_missing: article_id={ready.article_id}"
                    )
                return ExtractedOutcome(extraction=saved)

            # 同 tx に audit 焼付 (shape は audit_repository に閉じ込め)
            await ExtractionAuditRepository(session).append_extracted(
                ready=ready,
                envelope=envelope,
                code=ExtractedOutcome.CODE,
            )
            await session.commit()

            logger.info(
                "extraction_completed",
                article_id=ready.article_id,
                extraction_id=saved.id,
                entity_count=len(saved.entities),
            )
            return ExtractedOutcome(extraction=saved)

    async def _persist_noise(
        self,
        ready: ReadyForExtraction,
        envelope: ExtractionCall,
        ai_model: str,
    ) -> NoiseOutcome:
        async with self._session_factory() as session:
            noise_repo = NoiseRepository(session)
            saved = await noise_repo.save(
                envelope.result,
                article_id=ready.article_id,
                ai_model=ai_model,
            )

            if saved is None:
                # UNIQUE 違反による race 敗北 — 勝者を読み戻して合流する
                logger.info(
                    "extraction_noise_concurrent_write",
                    article_id=ready.article_id,
                )
                await session.commit()
                async with self._session_factory() as read_session:
                    saved = await NoiseRepository(read_session).find_by_article_id(
                        ready.article_id
                    )
                if saved is None:
                    raise RuntimeError(
                        f"extraction_noise_race_winner_missing: "
                        f"article_id={ready.article_id}"
                    )
                return NoiseOutcome()

            # 同 tx に audit 焼付 (shape は audit_repository に閉じ込め)
            await ExtractionAuditRepository(session).append_noise(
                ready=ready,
                envelope=envelope,
                code=NoiseOutcome.CODE,
            )
            await session.commit()

            logger.info(
                "extraction_noise_recorded",
                article_id=ready.article_id,
                noise_id=saved.id,
                entity_count=len(saved.entities),
            )
            return NoiseOutcome()

    async def mark_article_unprocessable(
        self,
        article_id: int,
        original_content: str,
        *,
        code: str,
        exc: BaseException,
    ) -> None:
        """内容起因 Permanent failure を 1 tx で焼付け + 記事 DELETE する。

        順序: **audit INSERT 先、DELETE 後** — A 級保険を最大化し、source_id
        の自動逆引きが Article 存在中に確定するように。FK は ``ondelete=
        SET NULL`` 設定済 (``pipeline_events.article_id``) のため DELETE 後も
        audit 行は残り、``source_id`` で起点ソースを追跡可能。

        Args:
            article_id: 削除対象記事 ID。
            original_content: 削除前に audit に焼く本文 (caller が読み出して
                渡す。Article DELETE 後は読めなくなるため)。
            code: ``type(exc).CODE`` (Layer 2 SSoT)。``outcome_code`` カラムにも
                同値を入れる (Phase A 規律、spec §PR 段取り)。
            exc: 例外インスタンス (audit の error_message / error_chain 用)。
        """
        async with self._session_factory() as session:
            # 1) audit INSERT (source_id 自動補完が article_id 健在時に確定)
            #    shape は audit_repository に閉じ込め
            await ExtractionAuditRepository(session).append_drop_article(
                article_id=article_id,
                original_content=original_content,
                code=code,
                exc=exc,
            )

            # 2) article DELETE (CASCADE で関連 row、SET NULL で audit.article_id)
            deleted = await ArticleRepository(session).delete_by_id(article_id)
            await session.commit()

            logger.warning(
                "extraction_article_unprocessable",
                article_id=article_id,
                code=code,
                deleted_rows=deleted,
                error_class=f"{type(exc).__module__}.{type(exc).__qualname__}",
            )
