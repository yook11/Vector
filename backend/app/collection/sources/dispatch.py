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

from app.collection.sources.errors import SourceNotRegisteredError
from app.collection.sources.fetch_cadence import FetchCadence
from app.collection.sources.registry import acquisition_source_for
from app.collection.sources.repository import RecordedSource, SourceRepository
from app.collection.sources.source_name import SourceName
from app.db.session import SessionFactory

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

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def select(self, cadence: FetchCadence | None) -> SourceDispatchSelection:
        """active source を選び cadence で絞り込んで返す。

        Args:
            cadence: 篩い tier。``None`` で全 tier (admin 手動 fetch 経路)。

        Returns:
            dispatch 対象と source 単位 rejection。``SOURCES`` に無いコード未登録
            source や source 名の不正は run 全体を落とさず rejection に畳む。
        """
        async with self._session_factory() as session:
            sources = await SourceRepository(session).list_active()
        targets: list[SourceDispatchTarget] = []
        rejections: list[SourceDispatchRejection] = []
        for source in sources:
            selection = _select_dispatch_target(source, cadence)
            if isinstance(selection, SourceDispatchTarget):
                targets.append(selection)
            elif isinstance(selection, SourceDispatchRejection):
                rejections.append(selection)
        return SourceDispatchSelection(
            targets=tuple(targets),
            rejections=tuple(rejections),
        )


def _select_dispatch_target(
    source: RecordedSource, cadence: FetchCadence | None
) -> SourceDispatchTarget | SourceDispatchRejection | None:
    """有効ソースの登録状態と頻度から、投入対象または棄却理由を返す。"""
    try:
        source_name = SourceName(source.raw_name)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "dispatch_source_name_invalid",
            source_id=source.id,
            raw_source_name=source.raw_name,
        )
        return SourceDispatchRejection(
            source_id=source.id,
            source_name=None,
            outcome_code=SourceDispatchRejectionCode.SOURCE_NAME_INVALID,
            raw_source_name=source.raw_name,
            exc=exc,
        )
    try:
        source_def = acquisition_source_for(source_name)
    except SourceNotRegisteredError:
        logger.warning("dispatch_source_unknown", source_name=str(source_name))
        return SourceDispatchRejection(
            source_id=source.id,
            source_name=str(source_name),
            outcome_code=SourceDispatchRejectionCode.SOURCE_NOT_REGISTERED,
        )
    if cadence is not None and source_def.fetch_cadence is not cadence:
        return None
    return SourceDispatchTarget(id=source.id, name=source_name)
