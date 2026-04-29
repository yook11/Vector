"""Extraction サービス — Stage C の処理組み立てと DB 永続化。

Pattern A' (spec §3.2 / §6.1 / §7) で `ReadyForExtraction` を Stage 間 passport
として受け取り、precondition (Article 存在 + Extraction 未生成 + 本文サイズ ≤
hard cap) は型レベルで構造保証されている。本サービスは:

- AI 呼び出し (session 外、slow IO 中の DB 接続専有を排除 — spec §4.7)
- save → race 敗北時の読戻し → Outcome 返却

の責務に縮退している。Outcome は ``ExtractedOutcome | InvalidInputOutcome`` の
2 variants。
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.errors import InvalidInputError
from app.analysis.extraction.domain import Extraction
from app.analysis.extraction.domain.ready import ReadyForExtraction
from app.analysis.extraction.extractor.base import BaseExtractor
from app.analysis.extraction.repository import ExtractionRepository

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ExtractedOutcome:
    """Stage C 成功 (新規 INSERT or race 敗北からの読戻し)。下流 Stage D に chain。"""

    extraction: Extraction


@dataclass(frozen=True, slots=True)
class InvalidInputOutcome:
    """AI が ``InvalidInputError`` を返した。chain しない。"""


ExtractionOutcome = ExtractedOutcome | InvalidInputOutcome


class ExtractionService:
    """1 記事の事実抽出と結果永続化を行うアトミックなユースケース。

    Stage C: 原文を読み、翻訳タイトル・事実ベース要約・エンティティを抽出する。
    分類（カテゴリ・トピック・インパクト）は Stage D の責務。
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

        Raises:
            AnalysisDomainError のサブクラス（InvalidInputError を除く）。
        """
        # AI 呼び出しは session 外 (slow IO 中の DB 接続専有を排除)
        try:
            result = await extractor.extract(
                title=ready.original_title,
                content=ready.original_content,
            )
        except InvalidInputError:
            logger.warning("extraction_invalid_input", article_id=ready.article_id)
            return InvalidInputOutcome()

        async with self._session_factory() as session:
            repo = ExtractionRepository(session)
            saved = await repo.save(
                result,
                article_id=ready.article_id,
                ai_model=extractor.model_name,
            )
            await session.commit()

            if saved is None:
                # race 敗北 — 勝者を読み戻して合流する
                logger.info(
                    "extraction_concurrent_write",
                    article_id=ready.article_id,
                )
                saved = await repo.find_by_article_id(ready.article_id)
                if saved is None:
                    raise RuntimeError(
                        f"extraction_race_winner_missing: article_id={ready.article_id}"
                    )

            logger.info(
                "extraction_completed",
                article_id=ready.article_id,
                extraction_id=saved.id,
                entity_count=len(saved.entities),
            )
            return ExtractedOutcome(extraction=saved)
