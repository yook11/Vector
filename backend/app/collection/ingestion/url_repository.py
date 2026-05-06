"""``article_urls`` テーブル向け Repository。

PR2.5-A 新設の ``article_urls`` (URL identity 台帳) への INSERT を
``ON CONFLICT (normalized_url) DO NOTHING`` で並行レース対応する。
``IngestionService`` が Stage 1 entry の振り分け先を決定する前に
1 件ずつ呼び出す。

設計:

- 戻り値は「新規に作られた id」または「既知 URL の場合 ``None``」。
  caller は ``None`` を ``known_url skipped`` としてカウントし、
  当該 entry の articles / pending_html_articles INSERT をスキップする。
  (既知 URL の id を読み戻すユースケースは Stage 1 では発生しない。
  必要になれば ``find_by_normalized_url`` を別途追加する。)
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article_url import ArticleUrl as ArticleUrlORM
from app.shared.value_objects.safe_url import SafeUrl


class ArticleUrlRepository:
    """``article_urls`` への INSERT (race-safe upsert)。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_returning(
        self,
        *,
        normalized_url: SafeUrl,
        original_url: SafeUrl,
        first_seen_source_id: int,
    ) -> int | None:
        """``article_urls`` に INSERT し、新規なら id を返す。既知 URL なら ``None``。

        ``ON CONFLICT (normalized_url) DO NOTHING RETURNING id`` で並行レース時の
        UNIQUE 違反を構造的に吸収する。caller は同一トランザクション内で結果を
        受け取り、``None`` のときは known_url としてスキップする。

        commit は呼び出し側 (Service) が行う。
        """
        stmt = (
            pg_insert(ArticleUrlORM)
            .values(
                normalized_url=normalized_url,
                original_url=original_url,
                first_seen_source_id=first_seen_source_id,
            )
            .on_conflict_do_nothing(index_elements=["normalized_url"])
            .returning(ArticleUrlORM.id)
        )
        row = (await self._session.execute(stmt)).first()
        return row.id if row is not None else None
