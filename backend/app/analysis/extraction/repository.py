"""Extraction リポジトリ — Stage C の永続化と読み出し。

責務:

- ``exists_for_article``: cheap な exists 判定 (Pattern A' の `try_advance_from`
  precondition チェック用)
- ``save``: 受け取った ``ExtractionResult`` を `INSERT ... ON CONFLICT (article_id)
  DO NOTHING RETURNING ...` で永続化する。race 敗北時 (rowcount=0) は ``None``
  を返し、Service が ``find_by_article_id`` で勝者を読み戻す (spec §4.6)。
  子テーブル (``article_entities``) の INSERT は親 INSERT 成功時のみ行う
  (race 敗北で orphan を作らない)。
- ``find_by_article_id``: ORM 行をドメイン Entity (``Extraction``) として復元する
  (永続化の双対 / race 敗北時の読戻し用)。
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import select

from app.analysis.extraction.domain import Entity, Extraction, ExtractionResult
from app.models.article_entity import ArticleEntity
from app.models.article_extraction import ArticleExtraction


class ExtractionRepository:
    """事実抽出（Stage C）に必要な DB 操作をカプセル化する。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists_for_article(self, article_id: int) -> bool:
        """`try_advance_from` 用 cheap exists 判定 (article_id 単位)。"""
        stmt = (
            select(ArticleExtraction.id)
            .where(ArticleExtraction.article_id == article_id)
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def find_by_article_id(self, article_id: int) -> Extraction | None:
        """記事に対する既存の抽出結果をドメイン Entity として取得する。"""
        stmt = (
            select(ArticleExtraction)
            .where(ArticleExtraction.article_id == article_id)
            .options(selectinload(ArticleExtraction.entities))
        )
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._to_domain(orm) if orm is not None else None

    async def save(
        self,
        result: ExtractionResult,
        *,
        article_id: int,
        ai_model: str,
    ) -> Extraction | None:
        """AI 分析結果を `INSERT ... ON CONFLICT (article_id) DO NOTHING
        RETURNING ...` で永続化する。

        commit は呼び出し側 (Service) が行う。``extracted_at`` は server_default で
        DB が確定させ RETURNING で受け取る。

        子テーブル ``article_entities`` の INSERT は親 INSERT 成功時のみ実施し、
        race 敗北で orphan エンティティを作らない。

        Returns:
            成功時: 永続化された ``Extraction`` Entity (id / extracted_at は DB 値、
            その他は result / 引数値)
            race 敗北時 (期待した article_id への UNIQUE 違反): ``None``
            (Service が `find_by_article_id` で勝者を読み戻す — spec §4.6)
        """
        stmt = (
            pg_insert(ArticleExtraction)
            .values(
                article_id=article_id,
                translated_title=result.title_ja,
                summary=result.summary_ja,
                ai_model=ai_model,
            )
            .on_conflict_do_nothing(index_elements=["article_id"])
            .returning(ArticleExtraction.id, ArticleExtraction.extracted_at)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None

        # 親 INSERT 成功時のみ子エンティティを INSERT する
        if result.entities:
            self._session.add_all(
                [
                    ArticleEntity(
                        article_extraction_id=row.id,
                        name=e.name,
                        type=e.type,
                    )
                    for e in result.entities
                ]
            )
            await self._session.flush()

        return Extraction(
            id=row.id,
            translated_title=result.title_ja,
            summary=result.summary_ja,
            entities=tuple(result.entities),
            ai_model=ai_model,
            extracted_at=row.extracted_at,
        )

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
