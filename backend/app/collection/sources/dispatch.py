"""Source dispatch decision Service — どの source を fetch すべきか決める。

本 Service は取得依頼を送らず、dispatch 対象を返すのみ。
依頼の送信は呼び出し側 (``SourceAcquisitionDispatcher``) の責務。

挙動:
- ``NewsSource`` テーブルから ``is_active=True`` の行を name 順で SELECT
- ``SOURCES`` dict (コード登録済 source 定義) で lookup できないものや source 名の
  不正は warning ログに出して除外する (failure-visibility のため非沈黙)
- ``cadence`` に一致する ``ArticleSource.fetch_cadence`` の source だけを返す
"""

from __future__ import annotations

from dataclasses import dataclass

from structlog.typing import FilteringBoundLogger

from app.collection.sources.errors import SourceNotRegisteredError
from app.collection.sources.fetch_cadence import FetchCadence
from app.collection.sources.registry import acquisition_source_for
from app.collection.sources.repository import RecordedSource, SourceRepository
from app.collection.sources.source_name import SourceName
from app.db.session import SessionFactory
from app.log_policy.policies.pipeline_control import PIPELINE_CONTROL_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger


@dataclass(frozen=True, slots=True)
class SourceDispatchTarget:
    """dispatch 対象の source 1 件分の VO。"""

    id: int
    name: SourceName


class SourceDispatchService:
    """active source を選び cadence で絞り込んだ結果を返す application service。

    依頼の送信は呼び出し側が行う。本 Service は「何を dispatch すべきか決める」
    だけのドメイン責任を担う。
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def select(self, cadence: FetchCadence) -> tuple[SourceDispatchTarget, ...]:
        """active source を選び cadence で絞り込んで返す。

        Args:
            cadence: 篩い tier。

        Returns:
            dispatch 対象。``SOURCES`` に無いコード未登録 source や source 名の
            不正は run 全体を落とさず warning ログに出して除外する。
        """
        async with self._session_factory() as session:
            sources = await SourceRepository(session).list_active()
        logger = create_policy_json_logger(__name__, PIPELINE_CONTROL_LOG_RULES)
        targets: list[SourceDispatchTarget] = []
        for source in sources:
            target = _select_dispatch_target(source, cadence, logger)
            if target is not None:
                targets.append(target)
        return tuple(targets)


def _select_dispatch_target(
    source: RecordedSource, cadence: FetchCadence, logger: FilteringBoundLogger
) -> SourceDispatchTarget | None:
    """有効ソースの登録状態と頻度から、投入対象を返す。"""
    try:
        source_name = SourceName(source.raw_name)
    except (TypeError, ValueError):
        logger.warning(
            "dispatch_source_name_invalid",
            source_id=source.id,
            raw_source_name=source.raw_name,
        )
        return None
    try:
        source_def = acquisition_source_for(source_name)
    except SourceNotRegisteredError:
        logger.warning("dispatch_source_unknown", source_name=str(source_name))
        return None
    if source_def.fetch_cadence is not cadence:
        return None
    return SourceDispatchTarget(id=source.id, name=source_name)
