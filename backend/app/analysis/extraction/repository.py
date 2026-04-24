"""Extraction リポジトリ — Stage 1 の永続化と読み出し。

責務は 2 つのみ:

- ``save``: 受け取った ``ExtractionResult`` を永続化し、DB が付与した identity
  (``PersistedId``) を返す。Entity の組み立ては呼び出し側 (Domain ファクトリ)
  に任せる。
- ``find_by_article_id``: DB から行を読み出してドメイン概念 (``Extraction``)
  として復元する (永続化の双対)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import select

from app.analysis.extraction.domain import Entity, Extraction, ExtractionResult
from app.models.article import Article
from app.models.article_entity import ArticleEntity
from app.models.article_extraction import ArticleExtraction


@dataclass(frozen=True, slots=True)
class PersistedId:
    """永続化で DB が付与した identity。

    ``save`` の戻り値。呼び出し側はこの値と元の ``ExtractionResult`` を
    ``Extraction.from_result`` に渡して記録済み Entity を組み立てる。
    """

    id: int
    extracted_at: datetime


class ExtractionRepository:
    """事実抽出（Stage 1）に必要な DB 操作をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_article_id(self, article_id: int) -> Extraction | None:
        """記事に対する既存の抽出結果をドメイン Entity として取得する。"""
        stmt = (
            select(ArticleExtraction)
            .where(ArticleExtraction.article_id == article_id)
            .options(selectinload(ArticleExtraction.entities))
        )
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._to_domain(orm) if orm is not None else None

    async def get_article(self, article_id: int) -> Article | None:
        """ID から記事を取得する。"""
        return await self._session.get(Article, article_id)

    async def save(
        self,
        result: ExtractionResult,
        *,
        article_id: int,
        ai_model: str,
    ) -> PersistedId:
        """AI 分析結果を受け取って永続化する。

        DB が付与した identity のみを返す。commit は呼び出し側が行う。
        """
        orm = ArticleExtraction(
            article_id=article_id,
            translated_title=result.title_ja,
            summary=result.summary_ja,
            ai_model=ai_model,
            entities=[ArticleEntity(name=e.name, type=e.type) for e in result.entities],
        )
        self._session.add(orm)
        await self._session.flush()
        # server_default の extracted_at を確定させる
        await self._session.refresh(orm, attribute_names=["extracted_at"])
        return PersistedId(id=orm.id, extracted_at=orm.extracted_at)

    @staticmethod
    def _to_domain(orm: ArticleExtraction) -> Extraction:
        """ORM から記録済み Entity へ復元する (読み出しの内部処理)。"""
        return Extraction(
            id=orm.id,
            translated_title=orm.translated_title,
            summary=orm.summary,
            entities=tuple(Entity(name=e.name, type=e.type) for e in orm.entities),
            ai_model=orm.ai_model,
            extracted_at=orm.extracted_at,
        )
