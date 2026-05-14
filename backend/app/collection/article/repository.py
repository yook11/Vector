"""``article`` aggregate の永続化と読み出し。

責務:

- ``ArticleRepository.save_ready``: ``ReadyForArticle`` (passport 型) を
  受け取って ``articles`` 行に直 INSERT し、新規採番された ``id`` を返す。
  即時獲得経路 (Pattern R) で ``ArticleAcquisitionService`` から呼ばれる。
  ``ON CONFLICT DO NOTHING`` で並行レース / 既知 URL を吸収し、新規行が
  作れなかった場合は ``None`` を返す。
- ``ArticleRepository.save``: ``ArticleDraft`` を ``articles`` 行に
  INSERT し、DB が採番した identity (``PersistedArticleId``) を返す。
  補完待ち獲得経路 (Pattern H) で ``ArticleCompletionService`` が使う。
  並行レースは ``INSERT ... ON CONFLICT DO NOTHING RETURNING`` で
  構造的に解消し、既に他ワーカーが書き込み済みなら ``None`` を返す。
- ``ArticleRepository.find_by_source_url``: 並行レース敗北時の
  読み戻し用に Article Entity を取得する。
- ``ArticleRepository.exists_by_source_url``: Pattern H ingestion の
  pre-check 用 (feed 再露出時に既知 URL の pending 化を回避し、HTML
  fetch の反復コストを抑える)。これはロックではなく実用上の
  idempotency で、同 tick race は ``save`` 側の
  ``ON CONFLICT DO NOTHING`` が吸収する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.collection.article.domain.article import Article, ArticleDraft, ReadyForArticle
from app.collection.article.domain.value_objects import PublishedAt
from app.models.article import Article as ArticleORM
from app.shared.value_objects.canonical_article_url import CanonicalArticleUrl


@dataclass(frozen=True, slots=True)
class PersistedArticleId:
    """``ArticleRepository.save`` が DB から受け取った identity 値。

    Service はこの値と元の ``ArticleDraft`` を ``Article.from_draft``
    に渡して記録済み Entity を組み立てる。
    """

    id: int
    created_at: datetime


def _article_from_orm(orm: ArticleORM) -> Article:
    """``ArticleORM`` から ``Article`` Entity への共通変換ヘルパ。

    Entity の不変条件 (id 正、title/body 非空) は
    ``Article.__post_init__`` が defense-in-depth として再検証する。
    """
    published_at = (
        PublishedAt(orm.published_at) if orm.published_at is not None else None
    )
    return Article(
        id=orm.id,
        title=orm.original_title,
        body=orm.original_content,
        published_at=published_at,
        created_at=orm.created_at,
    )


class ArticleRepository:
    """``Article`` 行の永続化と読み出し。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_ready(self, ready: ReadyForArticle) -> int | None:
        """``ReadyForArticle`` を ``articles`` に直 INSERT する。

        ``ON CONFLICT DO NOTHING`` で並行レース / 既知 URL を吸収し、
        新規行が作れなかった場合は ``None`` を返す。``source_url`` は
        ``CanonicalArticleUrl`` で canonical 性が構造保証されており、
        Repository 側での再正規化は不要 (``articles.source_url UNIQUE``
        は canonical 値で効く)。``SafeUrlType.process_bind_param`` が
        ``CanonicalArticleUrl`` を透過 bind する。commit は呼び出し側
        (Service) が行う。
        """
        stmt = (
            pg_insert(ArticleORM)
            .values(
                source_id=ready.source_id,
                source_url=ready.source_url,
                original_title=ready.title,
                original_content=ready.body,
                published_at=ready.published_at.value,
            )
            .on_conflict_do_nothing()
            .returning(ArticleORM.id)
        )
        row = (await self._session.execute(stmt)).first()
        return row.id if row is not None else None

    async def save(
        self,
        draft: ArticleDraft,
        *,
        source_id: int,
        source_url: CanonicalArticleUrl,
    ) -> PersistedArticleId | None:
        """``source_url`` 主軸で ``articles`` 行を INSERT する。

        ``ON CONFLICT DO NOTHING`` (制約ターゲット指定なし) で並行レース時の
        全 unique 違反を吸収する。``articles.source_url`` の UNIQUE で
        canonical URL の重複が構造的に弾かれる。``None`` を受けた Service は
        ``find_by_source_url`` で読み戻して合流させる。

        ``source_url`` の canonical 性は型 ``CanonicalArticleUrl`` で構造保証
        されているため Service / Repository での後付け正規化は不要。
        ORM 列は ``SafeUrl`` 表現だが ``SafeUrlType.process_bind_param`` が
        ``CanonicalArticleUrl`` を透過 bind する。commit は呼び出し側 (Service)
        が行う。
        """
        stmt = (
            pg_insert(ArticleORM)
            .values(
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

    async def find_by_source_url(
        self, source_url: CanonicalArticleUrl
    ) -> Article | None:
        """``source_url`` から既存 Article を Entity として取得する。

        Stage 2 の race-loss (``conflict_lost`` audit) 検出時の読み戻しに使う。
        ``CanonicalArticleUrl`` 型で canonical 性は構造保証されているため、
        UNIQUE 値とそのまま比較できる。
        """
        stmt = select(ArticleORM).where(ArticleORM.source_url == source_url)
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return _article_from_orm(orm) if orm is not None else None

    async def exists_by_source_url(self, source_url: CanonicalArticleUrl) -> bool:
        """``source_url`` を持つ ``articles`` 行が既に存在するかを軽量確認する。

        Pattern H ingestion の pre-check 用 (feed 再露出時に既知 URL の
        pending 化を回避し、HTML fetch の反復コストを抑える)。これはロックでは
        なく実用上の idempotency で、同 tick race は ``save`` 側の
        ``ON CONFLICT DO NOTHING`` が吸収する。
        """
        stmt = select(ArticleORM.id).where(ArticleORM.source_url == source_url).limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None
