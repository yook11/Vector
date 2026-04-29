"""ReadyForDigest — Stage F 実行可能状態の precondition 型 (Pattern A')。

spec `specs/typed-pipeline-preconditions.md` §5 / §6.3 / §7 で確定した設計の digest
BC 実装。Stage F は **入口 task (cron 駆動)** であり Stage 間 passport ではない。
Ready は cron 引数 (week_start + force) の構造化と precondition (snapshot 未生成)
の表明を担う。

`@dataclass(frozen=True, slots=True)` ではなく `BaseModel(frozen=True)` を使う
理由: Phase 1-3 と統一し、将来の手動 enqueue 経路 (kiq に Ready を渡す) で
taskiq の formatter (Pydantic ベース) と整合させるため (taskiq Issue #441 / #558、
memory `feedback_taskiq_basemodel_required.md`)。本 Stage では現状 cron 駆動のみ
だが、Phase 5 の入口 task pattern と統一するために BaseModel を採用する。
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, Self

import structlog
from pydantic import BaseModel, ConfigDict, model_validator

logger = structlog.get_logger(__name__)


class SnapshotExistenceProtocol(Protocol):
    """Stage F 進行判定用 Snapshot Repository contract (cheap exists 判定)。"""

    async def exists_for_week(self, week_start: date) -> bool: ...


class ReadyForDigest(BaseModel):
    """Stage F digest を実行可能な状態を表す precondition 型。

    入口 task の cron 引数を構造化する型として機能する (Phase 1-3 の Stage 間
    passport とは性格が異なる)。

    Invariants:
    - ``week_start``: JST 週境界 (月曜) — ``model_validator`` で
      ``weekday() == 0`` を構造的に保証
    - ``force``: 既存 snapshot を上書きする意図を持つかの明示フラグ
    - frozen: 生成後は不変
    """

    model_config = ConfigDict(frozen=True)

    week_start: date
    force: bool = False

    @model_validator(mode="after")
    def _ensure_monday(self) -> Self:
        """``week_start`` が JST 週境界 (月曜) であることを構造的に保証する。

        ``WEEK_TZ = "Asia/Tokyo"`` のもとで ``date.weekday() == 0`` (Monday) を
        check する。tz は ``date`` 型では表現できないため、呼び出し側 (Task /
        CLI / `latest_completed_week_start`) が JST で算出することを前提とする。
        """
        if self.week_start.weekday() != 0:
            raise ValueError(
                f"week_start must be a Monday (JST), got {self.week_start} "
                f"(weekday={self.week_start.weekday()})"
            )
        return self

    @classmethod
    async def try_advance_from(
        cls,
        *,
        week_start: date,
        force: bool,
        snapshot_repo: SnapshotExistenceProtocol,
    ) -> ReadyForDigest | None:
        """Stage F へ advance できるかを判定する gatekeeper。

        Phase 1-3 の単一 source-Entity 引数とは異なり kwargs 経路を採る。Stage F
        には Ready 構築の起点となる domain Entity が存在せず、入口 task で
        外部入力 (cron / CLI 引数) から直接構築するため。

        Precondition:
        - ``force=False`` の場合: 同 ``week_start`` の snapshot 未生成
        - ``force=True`` の場合: 既存有無を問わず通す (上書き経路)

        Returns:
            進める場合: ``ReadyForDigest``
            進めない場合: ``None`` (業務正常状態、例外ではない — 既存 snapshot あり
            + force=False。spec §4.5 Failure mode 1)

        Note:
            `cls()` 構築を最初に行うことで ``model_validator`` (月曜判定) を
            外部 Repository 呼び出しより先に発火させ、無効入力で DB を叩かない。
        """
        candidate = cls(week_start=week_start, force=force)
        if not force and await snapshot_repo.exists_for_week(week_start):
            logger.info(
                "digest_skipped_existing_snapshot",
                week_start=week_start.isoformat(),
            )
            return None
        return candidate
