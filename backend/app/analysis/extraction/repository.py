"""Extraction リポジトリ — Stage C の永続化と読み出し。

責務:

- ``exists_for_article``: cheap な exists 判定 (Pattern A' の `try_advance_from`
  precondition チェック用)
- ``save``: 受け取った ``ExtractionResult`` を `INSERT ... ON CONFLICT (article_id)
  DO NOTHING RETURNING ...` で永続化する。race 敗北時 (rowcount=0) は ``None``
  を返し、Service が ``find_by_article_id`` で勝者を読み戻す (spec §4.6)。
  子テーブル (``article_extraction_entities``) の INSERT は親 INSERT 成功時の
  みで race 敗北による orphan を作らない。
- ``find_by_article_id``: ORM 行をドメイン Entity (``Extraction``) として復元する
  (永続化の双対 / race 敗北時の読戻し用)。
"""

from __future__ import annotations

from sqlalchemy import delete, func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import select

from app.analysis.extraction.domain import (
    ExtractedEntity,
    Extraction,
    ExtractionResult,
)
from app.models.article_extraction import ArticleExtraction
from app.models.article_extraction_entity import ArticleExtractionEntity


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

        子テーブル ``article_extraction_entities`` の INSERT は親 INSERT 成功時
        のみ実施し、race 敗北で orphan エンティティを作らない。

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

        # 親 INSERT 成功時のみ子エンティティを INSERT する。``position`` は AI 出力
        # 順を保存し、後段で人間レビュー時に prompt 出力順を再現できるようにする。
        if result.entities:
            self._session.add_all(
                [
                    ArticleExtractionEntity(
                        extraction_id=row.id,
                        surface=e.surface,
                        raw_type=e.raw_type,
                        position=i,
                    )
                    for i, e in enumerate(result.entities)
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

    async def update_idempotent(
        self,
        result: ExtractionResult,
        *,
        article_id: int,
        ai_model: str,
    ) -> Extraction:
        """既存の Extraction を新しい ``ExtractionResult`` で上書きする (CLI 用)。

        Phase 1B α-1 の re-extraction CLI 専用。再現性を持たせるため:

        - 親 ``ArticleExtraction`` は **UPDATE のみ** (DELETE しない)。これにより
          ``in_scope_assessments`` / ``out_of_scope_assessments`` /
          ``article_embeddings`` / ``watchlist_entries`` への CASCADE 連鎖を
          構造的に回避する
          (parent DELETE するとユーザの watchlist が消失するため)。
        - 子 ``article_extraction_entities`` のみ DELETE → INSERT で差し替える
          (新 prompt の出力を新 schema にそのまま流し込む)。
        - ``extracted_at`` は ``func.now()`` で再採番する (再抽出した時刻として
          扱い、後段の運用で「いつ抽出された」を取り違えない)。

        対象 article_id に対する extraction が存在しない前提 (CLI 側で事前に
        ``exists_for_article`` で絞り込む)。存在しない場合は ``NoResultFound``。
        """
        update_stmt = (
            update(ArticleExtraction)
            .where(ArticleExtraction.article_id == article_id)
            .values(
                translated_title=result.title_ja,
                summary=result.summary_ja,
                ai_model=ai_model,
                extracted_at=func.now(),
            )
            .returning(ArticleExtraction.id, ArticleExtraction.extracted_at)
        )
        row = (await self._session.execute(update_stmt)).one()

        await self._session.execute(
            delete(ArticleExtractionEntity).where(
                ArticleExtractionEntity.extraction_id == row.id
            )
        )

        if result.entities:
            self._session.add_all(
                [
                    ArticleExtractionEntity(
                        extraction_id=row.id,
                        surface=e.surface,
                        raw_type=e.raw_type,
                        position=i,
                    )
                    for i, e in enumerate(result.entities)
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
            entities=tuple(
                ExtractedEntity(surface=e.surface, raw_type=e.raw_type)
                for e in orm.entities
            ),
            ai_model=orm.ai_model,
            extracted_at=orm.extracted_at,
        )
