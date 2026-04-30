"""Extraction リポジトリ — DiscoveredArticle ルックアップと Article 永続化。

責務:

- ``DiscoveredArticleLookupRepository``: 抽出対象の ``DiscoveredArticle`` を
  ``DiscoveredLookup`` VO として返す。Article ORM はここで Entity に変換し、
  Service へは ORM を出さない。
- ``ArticleRepository.save``: ``ArticleDraft`` を ``articles`` 行に INSERT し、
  DB が採番した identity (``PersistedArticleId``) を返す。
  ``UNIQUE(discovered_article_id)`` の並行レースは
  ``INSERT ... ON CONFLICT DO NOTHING RETURNING`` で構造的に解消し、
  既に他ワーカーが書き込み済みなら ``None`` を返す。
- ``ArticleRepository.find_by_discovered_article_id``: 並行レース敗北時の
  読み戻し用に Article Entity を取得する。

ingestion 側にも同名の ``DiscoveredArticleRepository`` (URL 重複排除責務)
が存在する。本 BC は責務が異なるため ``DiscoveredArticleLookupRepository``
として明示的に分離する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlmodel import select

from app.collection.extraction.domain import Article, PublishedAt
from app.collection.extraction.domain.article import ArticleDraft
from app.models.article import Article as ArticleORM
from app.models.discovered_article import DiscoveredArticle as DiscoveredArticleORM
from app.shared.value_objects.safe_url import SafeUrl


@dataclass(frozen=True, slots=True)
class DiscoveredLookup:
    """``DiscoveredArticle`` のルックアップ結果 VO。

    Service が抽出可否を判定するための最小集合: identity (``id``) +
    抽出対象 URL (``original_url``) + 既存 Article の有無
    (``existing_article``)。ORM を Service に出さないための DTO 兼境界 VO。

    NOTE: ingestion BC が DDD 化されるまでの暫定 VO。ingestion 側の Entity
    (DiscoveredArticle) と統合された時点で削除予定。
    """

    id: int
    news_source_id: int
    original_url: SafeUrl
    existing_article: Article | None


@dataclass(frozen=True, slots=True)
class PersistedArticleId:
    """``ArticleRepository.save`` が DB から受け取った identity 値。

    Service はこの値と元の ``ArticleDraft`` を ``Article.from_draft`` に
    渡して記録済み Entity を組み立てる。
    """

    id: int
    created_at: datetime


def _article_from_orm(orm: ArticleORM) -> Article:
    """``ArticleORM`` から ``Article`` Entity への共通変換。

    ``DiscoveredArticleLookupRepository`` と ``ArticleRepository`` の両方が
    使うため module-level に切り出している。Entity の不変条件 (id 正・非空)
    は ``Article.__post_init__`` が defense-in-depth として再検証する。
    """
    published_at = (
        PublishedAt(orm.published_at) if orm.published_at is not None else None
    )
    return Article(
        id=orm.id,
        discovered_article_id=orm.discovered_article_id,
        title=orm.original_title,
        body=orm.original_content,
        published_at=published_at,
        created_at=orm.created_at,
    )


class DiscoveredArticleLookupRepository:
    """抽出対象の ``DiscoveredArticle`` をルックアップする。

    ingestion 側の ``DiscoveredArticleRepository`` (URL 重複排除責務) とは
    解いている問題が異なる (こちらは抽出対象ルックアップ)。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_id(self, discovered_article_id: int) -> DiscoveredLookup | None:
        """ID で ``DiscoveredArticle`` をルックアップして ``DiscoveredLookup`` を返す。

        既存 Article の有無判定用に ``article`` リレーションを ``selectinload``
        で事前取得し、1 ラウンドトリップで完結させる。
        """
        stmt = (
            select(DiscoveredArticleORM)
            .where(DiscoveredArticleORM.id == discovered_article_id)
            .options(selectinload(DiscoveredArticleORM.article))
        )
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return self._to_lookup(orm) if orm is not None else None

    @staticmethod
    def _to_lookup(orm: DiscoveredArticleORM) -> DiscoveredLookup:
        """ORM から VO への内部変換。"""
        existing = _article_from_orm(orm.article) if orm.article is not None else None
        return DiscoveredLookup(
            id=orm.id,
            news_source_id=orm.news_source_id,
            original_url=orm.original_url,
            existing_article=existing,
        )


class ArticleRepository:
    """``Article`` 行の永続化と読み出し。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(
        self,
        draft: ArticleDraft,
        *,
        discovered_article_id: int,
        source_id: int,
        source_url: SafeUrl,
    ) -> PersistedArticleId | None:
        """``ArticleDraft`` を ``articles`` 行に INSERT する (並行レース対応)。

        ``ON CONFLICT DO NOTHING`` (制約ターゲット指定なし) で並行レース時の
        全 unique 違反を吸収する。``articles`` には UNIQUE が 2 本ある:

        - ``uq_articles_discovered_article_id`` — 同一 discovered からの二重抽出
        - ``uq_articles_source_url`` — 異なる discovered が canonical URL の
          正規化結果として同一 ``source_url`` に行き当たるケース (Phase 1+)

        どちらの conflict でも race recovery の意味論は「他者が先に書き込み済み」
        で同じため、ターゲットを限定せず両方を吸収する。``None`` を受けた
        Service は ``find_by_discovered_article_id`` で読み戻して合流させる。

        ``source_id`` / ``source_url`` は caller (Service) が ``DiscoveredLookup``
        から渡す: 同一トランザクション内で既知の値であり追加 SELECT 不要。
        Phase 0b で NOT NULL + UNIQUE 化されたため必須引数。

        commit は呼び出し側 (Service) が行う。
        """
        stmt = (
            pg_insert(ArticleORM)
            .values(
                discovered_article_id=discovered_article_id,
                source_id=source_id,
                source_url=source_url,
                original_title=draft.title,
                original_content=draft.body,
                published_at=(
                    draft.published_at.value if draft.published_at is not None else None
                ),
            )
            .on_conflict_do_nothing()
            .returning(ArticleORM.id, ArticleORM.created_at)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        return PersistedArticleId(id=row.id, created_at=row.created_at)

    async def find_by_discovered_article_id(
        self, discovered_article_id: int
    ) -> Article | None:
        """``discovered_article_id`` から既存 Article を Entity として取得する。

        並行レース敗北時 (``save`` が ``None`` を返したとき) の読み戻しと、
        defense-in-depth の冪等性検証で使う。
        """
        stmt = select(ArticleORM).where(
            ArticleORM.discovered_article_id == discovered_article_id
        )
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return _article_from_orm(orm) if orm is not None else None
