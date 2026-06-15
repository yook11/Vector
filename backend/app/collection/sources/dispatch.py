"""Source dispatch decision Service — どの source を fetch すべきか決める。

`.kiq()` (queue 依存) は task 側に置く設計のため、本 Service は kiq enqueue を
行わず、dispatch 対象と source 単位 rejection を返すのみ。実 enqueue は
呼び出し側 (cron task) の責務。

挙動:
- ``NewsSource`` テーブルから ``is_active=True`` の行を name 順で SELECT
- ``SOURCES`` dict (コード登録済 source 定義) で lookup できないものは rejection
  として返す (failure-visibility のため非沈黙)
- ``cadence`` が指定されていれば ``ArticleSource.fetch_cadence`` で篩い、
  ``None`` なら全 tier を返す (admin 手動 fetch 経路)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import structlog
from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collection.sources.fetch_cadence import FetchCadence
from app.collection.sources.source_name import SourceName
from app.models.news_source import NewsSource

logger = structlog.get_logger(__name__)


class SourceDispatchRejectionCode(StrEnum):
    """source 単位で dispatch 対象から除外した理由。"""

    SOURCE_NOT_REGISTERED = "source_not_registered"
    SOURCE_NAME_INVALID = "source_name_invalid"


@dataclass(frozen=True, slots=True)
class SourceDispatchTarget:
    """dispatch 対象の source 1 件分の VO。

    queue task が本 VO を受け取り、kiq message DTO (``AcquireSourceTaskInput``) に
    変換して ``.kiq()`` を呼ぶ。Service は kiq に触れない (queue 依存を持たない)。
    """

    id: int
    name: SourceName


@dataclass(frozen=True, slots=True)
class SourceDispatchRejection:
    """source 単位で dispatch 対象から除外した事実。"""

    source_id: int | None
    source_name: str | None
    outcome_code: SourceDispatchRejectionCode
    raw_source_name: str | None = None
    exc: BaseException | None = None


@dataclass(frozen=True, slots=True)
class SourceDispatchSelection:
    """dispatch 対象と source 単位 rejection の選定結果。"""

    targets: tuple[SourceDispatchTarget, ...]
    rejections: tuple[SourceDispatchRejection, ...]


class SourceDispatchService:
    """active source を選び cadence で絞り込んだ結果を返す application service。

    kiq enqueue は呼び出し側 (cron task) が行う。本 Service は「何を dispatch
    すべきか決める」だけのドメイン責任を担う。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def select(self, cadence: FetchCadence | None) -> SourceDispatchSelection:
        """active source を選び cadence で絞り込んで返す。

        Args:
            cadence: 篩い tier。``None`` で全 tier (admin 手動 fetch 経路)。

        Returns:
            dispatch 対象と source 単位 rejection。``SOURCES`` に無いコード未登録
            source や source 名の不正は run 全体を落とさず rejection に畳む。
        """
        # SOURCES は import が重いため lazy (scheduler の import を軽く保つ)。
        from app.collection.article_acquisition.strategy import SOURCES

        async with self._session_factory() as session:
            raw_name = cast(NewsSource.name, String).label("raw_name")
            rows = list(
                (
                    await session.execute(
                        select(NewsSource.id, raw_name)
                        .where(NewsSource.is_active == True)  # noqa: E712
                        .order_by(raw_name)
                    )
                ).all()
            )

        targets: list[SourceDispatchTarget] = []
        rejections: list[SourceDispatchRejection] = []
        for row in rows:
            raw_source_name = _raw_source_name(row)
            try:
                source_name = SourceName(raw_source_name)
            except (TypeError, ValueError) as exc:
                rejections.append(
                    SourceDispatchRejection(
                        source_id=row.id,
                        source_name=None,
                        outcome_code=SourceDispatchRejectionCode.SOURCE_NAME_INVALID,
                        raw_source_name=(
                            raw_source_name
                            if isinstance(raw_source_name, str)
                            else repr(raw_source_name)
                        ),
                        exc=exc,
                    )
                )
                logger.warning(
                    "dispatch_source_name_invalid",
                    source_id=row.id,
                    raw_source_name=raw_source_name,
                )
                continue
            source_def = SOURCES.get(source_name)
            if source_def is None:
                rejections.append(
                    SourceDispatchRejection(
                        source_id=row.id,
                        source_name=str(source_name),
                        outcome_code=SourceDispatchRejectionCode.SOURCE_NOT_REGISTERED,
                    )
                )
                logger.warning("dispatch_source_unknown", source_name=str(source_name))
                continue
            if cadence is not None and source_def.fetch_cadence is not cadence:
                continue
            targets.append(SourceDispatchTarget(id=row.id, name=source_name))
        return SourceDispatchSelection(
            targets=tuple(targets),
            rejections=tuple(rejections),
        )


def _raw_source_name(row: object) -> object:
    """SQLAlchemy Row / test double の raw source name を取り出す。"""
    return getattr(row, "raw_name", getattr(row, "name", None))
