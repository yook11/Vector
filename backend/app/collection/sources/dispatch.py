"""Source dispatch decision Service — どの source を fetch すべきか決める。

`.kiq()` (queue 依存) は task 側に置く設計のため、本 Service は kiq enqueue を
行わず、dispatch 対象の VO リストを返すのみ。実 enqueue は呼び出し側 (cron task)
の責務。

挙動:
- ``NewsSource`` テーブルから ``is_active=True`` の行を name 順で SELECT
- ``SOURCES`` dict (コード登録済 source 定義) で lookup できないものは warning
  + skip (failure-visibility のため非沈黙)
- ``cadence`` が指定されていれば ``ArticleSource.fetch_cadence`` で篩い、
  ``None`` なら全 tier を返す (admin 手動 fetch 経路)
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.collection.sources.fetch_cadence import FetchCadence
from app.models.news_source import NewsSource
from app.shared.value_objects.source_name import SourceName

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SourceDispatchTarget:
    """dispatch 対象の source 1 件分の VO。

    queue task が本 VO を受け取り、kiq message DTO (``AcquireSourceArg``) に
    変換して ``.kiq()`` を呼ぶ。Service は kiq に触れない (queue 依存を持たない)。
    """

    id: int
    name: SourceName


class SourceDispatchService:
    """active source を選び cadence で絞り込んだリストを返す application service。

    kiq enqueue は呼び出し側 (cron task) が行う。本 Service は「何を dispatch
    すべきか決める」だけのドメイン責任を担う。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def select(self, cadence: FetchCadence | None) -> list[SourceDispatchTarget]:
        """active source を選び cadence で絞り込んで返す。

        Args:
            cadence: 篩い tier。``None`` で全 tier (admin 手動 fetch 経路)。

        Returns:
            dispatch 対象の VO リスト。``SOURCES`` に無いコード未登録 source は
            warning を出して除外する (steady-state でない異常状態として観測)。
        """
        # SOURCES は import が重いため lazy (scheduler の import を軽く保つ)。
        from app.collection.article_acquisition.strategy import SOURCES

        async with self._session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(NewsSource.id, NewsSource.name)
                        .where(NewsSource.is_active == True)  # noqa: E712
                        .order_by(NewsSource.name)
                    )
                ).all()
            )

        targets: list[SourceDispatchTarget] = []
        for row in rows:
            source_name = SourceName(row.name)
            source_def = SOURCES.get(source_name)
            if source_def is None:
                logger.warning("dispatch_source_unknown", source_name=str(source_name))
                continue
            if cadence is not None and source_def.fetch_cadence is not cadence:
                continue
            targets.append(SourceDispatchTarget(id=row.id, name=source_name))
        return targets
