"""Extraction サービス — Stage 3 の処理組み立てと DB 永続化。

Pattern A' (spec §3.2 / §6.1 / §7) で ``ReadyForExtraction`` を Stage 間 passport
として受け取り、precondition (Article 存在 + Extraction/Noise 未生成 + 本文
サイズ ≤ hard cap) は型レベルで構造保証されている。本サービスは:

- AI 呼び出し (session 外、slow IO 中の DB 接続専有を排除 — spec §4.7)
- ``relevance`` で signal/noise を振り分け、それぞれ別テーブルへ永続化
- race 敗北時 (Repository が ``None`` 返却) は audit / commit を焼かず短絡
  (勝者 SSoT、Stage 4 AssessmentService / Stage 5 EmbeddingService と同型)
- 各成功経路で同 tx に audit を焼付 (``ExtractionAuditRepository`` の
  ``append_extracted`` / ``append_noise`` を呼ぶだけ、shape は repository SSoT)
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

        precondition は ``ReadyForExtraction`` で構造保証済 (Article 存在 +
        Extraction 未生成 + 本文サイズ ≤ hard cap)。本メソッド内で再 fetch
        / None check は行わない。

        ``match envelope: case ExtractionCall(result=Signal()): | case
        ExtractionCall(result=Noise()):`` の dispatch は Generic envelope に
        対する型 narrowing をそのまま使う (Stage 4 AssessmentService と対称)。

        失敗は全て Layer 2 例外として **そのまま raise** する (Service は catch
        しない)。Task 層が Layer 1 marker で dispatch する責務 (spec §原則 4)。

        Returns:
            signal 勝者: 新規 ``article_extractions.id`` (``int``、Task 層が
                Stage 4 ``assess_content.kiq`` に渡す)
            noise 勝者 / signal race 敗北 / noise race 敗北: ``None`` (Task 層は
                chain せず short return)

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

        match envelope:
            case ExtractionCall(result=Signal()):
                # ``envelope`` は ``ExtractionCall[Signal]`` に narrow される
                return await self._persist_signal(ready, envelope)
            case ExtractionCall(result=Noise()):
                # ``envelope`` は ``ExtractionCall[Noise]`` に narrow される
                return await self._persist_noise(ready, envelope)
            case _:
                assert_never(envelope)

    async def _persist_signal(
        self,
        ready: ReadyForExtraction,
        envelope: ExtractionCall[Signal],
    ) -> int | None:
        """signal 経路: 勝者なら audit + commit し ``extraction_id`` を返す。

        Repository が ``None`` を返した race 敗北時は audit / commit を焼かず
        短絡する (勝者 SSoT、Stage 4 と同型)。
        """
        async with self._session_factory() as session:
            repo = ExtractionRepository(session)
            extraction_id = await repo.save_signal(
                envelope, article_id=ready.article_id
            )

            if extraction_id is None:
                # race 敗北 — 勝者 task が audit を焼く責務を持つので何もしない
                logger.info(
                    "extract_race_loss_signal",
                    article_id=ready.article_id,
                )
                return None

            # 同 tx に audit 焼付 (shape は audit_repository に閉じ込め)
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

    async def _persist_noise(
        self,
        ready: ReadyForExtraction,
        envelope: ExtractionCall[Noise],
    ) -> None:
        """noise 経路: 勝者なら audit + commit し ``None`` を返す。

        Stage 4 chain は noise 勝者でも発火させないため、Repository の戻り値が
        ``int`` (勝者) であっても Service は ``None`` を返す。Task 層は uniform に
        ``if result is None: return`` で chain 抑止する。

        race 敗北時 (Repository が ``None``) は audit / commit を焼かず短絡する。
        """
        async with self._session_factory() as session:
            repo = ExtractionRepository(session)
            noise_id = await repo.save_noise(envelope, article_id=ready.article_id)

            if noise_id is None:
                # race 敗北 — 勝者 task が audit を焼く責務を持つので何もしない
                logger.info(
                    "extract_race_loss_noise",
                    article_id=ready.article_id,
                )
                return None

            # 同 tx に audit 焼付 (shape は audit_repository に閉じ込め)
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
            return None

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
