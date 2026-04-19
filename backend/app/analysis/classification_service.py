"""Classification サービス — Stage 2 の処理組み立てと DB 永続化。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.classifier.base import BaseClassifier
from app.analysis.errors import ProviderError
from app.analysis.extractor.base import EntityData
from app.analysis.repository import AnalysisRepository
from app.models.article_entity import EntityType
from app.utils.sanitize import strip_html_tags

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ClassificationResult:
    """Stage 2 分類ユースケースの結果。"""

    status: Literal["classified", "already_classified", "skipped"]


class ClassificationService:
    """1 記事の分類と結果永続化を行うアトミックなユースケース。

    Stage 2: Stage 1 の構造化出力（DB から読み出し）に対して分類を実行する。
    原文は読まない。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(
        self, article_id: int, classifier: BaseClassifier
    ) -> ClassificationResult:
        """1 記事に対して分類を実行する。

        Returns:
            status を含む ClassificationResult。

        Raises:
            AnalysisDomainError のサブクラス。
        """
        async with self._session_factory() as session:
            repo = AnalysisRepository(session)

            # ArticleAnalysis を取得（Stage 1 未完了なら skip）
            analysis = await repo.find_by_article_id(article_id)
            if analysis is None:
                logger.warning(
                    "classification_analysis_not_found", article_id=article_id
                )
                return ClassificationResult("skipped")

            # 冪等性チェック（分類済みならスキップ）
            if analysis.topic_id is not None:
                return ClassificationResult("already_classified")

            # DB からエンティティ読み出し
            db_entities = await repo.get_entities_by_analysis_id(analysis.id)
            entities = [
                EntityData(name=e.name, type=EntityType(e.type)) for e in db_entities
            ]

            # 既存トピック取得（プロンプトガイド用）
            existing_topics = await repo.get_existing_topics_by_category()

            # AI による分類
            data = await classifier.classify(
                title_ja=analysis.translated_title,
                summary_ja=analysis.summary,
                entities=entities,
                existing_topics_by_category=existing_topics,
            )

            # カテゴリ ID を取得
            category_id = await repo.get_category_id_by_slug(data.category_slug)
            if category_id is None:
                raise ProviderError(
                    f"AI returned unknown category slug: {data.category_slug!r}"
                )

            # Topic の find-or-create
            topic_id = await repo.find_or_create_topic(data.topic_name, category_id)

            # ArticleAnalysis 更新
            analysis.topic_id = topic_id
            analysis.impact_level = data.impact_level
            analysis.reasoning = strip_html_tags(data.reasoning) or ""

            await session.commit()

            logger.info(
                "classification_completed",
                article_id=article_id,
                impact_level=data.impact_level,
                category=data.category_slug,
                topic=data.topic_name,
            )
            return ClassificationResult("classified")
