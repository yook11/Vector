"""``source_id`` / ``sourceName`` → ``SourceCompletionProfile`` 解決 seam。

Stage 2 の pending 行は ``source_id`` (FK) しか持たない。新行は
``ObservedArticle.source_name`` を持つが、in-flight 旧行は持たない (spec §5)。
repository (ACL) は本 Protocol にのみ依存し、composition root ``SOURCES`` を
import しない (spec §4.6 ガードレール 1)。具象 ``Registry...`` が
``SOURCES`` 引き + ``source_id → news_sources.name`` DB フォールバックを内包
する (session を持つのは ACL なので解決もここに集約する)。
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collection.domain.source_completion_profile import (
    DEFAULT_PROFILE,
    SourceCompletionProfile,
)
from app.collection.source_fetch.strategy import SOURCES
from app.models.news_source import NewsSource as NewsSourceORM
from app.shared.value_objects.source_name import SourceName


class CompletionProfileResolver(Protocol):
    """補完方針 / ソース名の解決契約 (repository が依存する seam)。"""

    async def resolve(
        self, *, source_id: int, source_name: SourceName | None
    ) -> SourceCompletionProfile: ...

    async def resolve_name(self, *, source_id: int) -> SourceName: ...


class RegistryCompletionProfileResolver:
    """``SOURCES`` 引き + ``source_id→name`` DB フォールバックの具象。

    ``SOURCES`` を import するのは本ファイルのみ。repository は
    ``CompletionProfileResolver`` Protocol にのみ依存する。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_name(self, *, source_id: int) -> SourceName:
        """``news_sources.name`` を引く (legacy 行の identity 補完用)。

        pending 行は ``source_id`` FK (RESTRICT) を持つため通常ヒットする。
        万一不在なら data integrity 違反として ``ValueError``。
        """
        stmt = select(NewsSourceORM.name).where(NewsSourceORM.id == source_id)
        row = (await self._session.execute(stmt)).first()
        if row is None:
            msg = f"news_source not found: id={source_id}"
            raise ValueError(msg)
        return row.name

    async def resolve(
        self, *, source_id: int, source_name: SourceName | None
    ) -> SourceCompletionProfile:
        """補完方針を解決する。``source_name`` 欠落 (旧行) は
        ``source_id`` から DB 解決する。未登録ソースは ``DEFAULT_PROFILE``。
        """
        name = (
            source_name
            if source_name is not None
            else await self.resolve_name(source_id=source_id)
        )
        # ``SOURCES`` は ``SourceName → ArticleSource`` (= Source クラス
        # オブジェクト)。``.completion_profile`` はクラス属性直読み。Source は
        # class そのものが Protocol を満たし ``adapter_factory`` /
        # ``make_adapter`` のような構築経路が存在しないため、profile 読みで
        # machinery を作る経路が構造的に不能 (class-ref 無 instantiation 保証)。
        source = SOURCES.get(SourceName(str(name)))
        return source.completion_profile if source is not None else DEFAULT_PROFILE
