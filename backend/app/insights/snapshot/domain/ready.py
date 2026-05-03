"""ReadyForDigest — Stage F 実行可能状態の precondition 型 (Pattern A')。

spec `specs/typed-pipeline-preconditions.md` §5 / §6.3 / §7 で確定した設計の digest
BC 実装。Stage F は **入口 task (cron 駆動)** であり Stage 間 passport ではない。
Ready は cron 引数 (window_end + force) の構造化と precondition (snapshot 未生成)
の表明を担う。

`@dataclass(frozen=True, slots=True)` ではなく `BaseModel(frozen=True)` を使う
理由: Phase 1-3 と統一し、将来の手動 enqueue 経路 (kiq に Ready を渡す) で
taskiq の formatter (Pydantic ベース) と整合させるため (taskiq Issue #441 / #558、
memory `feedback_taskiq_basemodel_required.md`)。本 Stage では現状 cron 駆動のみ
だが、Phase 5 の入口 task pattern と統一するために BaseModel を採用する。
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

import structlog
from pydantic import BaseModel, ConfigDict

logger = structlog.get_logger(__name__)


class SnapshotExistenceProtocol(Protocol):
    """Stage F 進行判定用 Snapshot Repository contract (cheap exists 判定)。"""

    async def exists_for_window_end(self, window_end: date) -> bool: ...


class ReadyForDigest(BaseModel):
    """Stage F digest を実行可能な状態を表す precondition 型。

    入口 task の cron 引数を構造化する型として機能する (Phase 1-3 の Stage 間
    passport とは性格が異なる)。

    Invariants:
    - ``window_end``: rolling 7d window の上限 (任意の JST 日付)
    - ``force``: 既存 snapshot を上書きする意図を持つかの明示フラグ
    - frozen: 生成後は不変
    """

    model_config = ConfigDict(frozen=True)

    window_end: date
    force: bool = False

    @classmethod
    async def try_advance_from(
        cls,
        *,
        window_end: date,
        force: bool,
        snapshot_repo: SnapshotExistenceProtocol,
    ) -> ReadyForDigest | None:
        """Stage F へ advance できるかを判定する gatekeeper。

        Phase 1-3 の単一 source-Entity 引数とは異なり kwargs 経路を採る。Stage F
        には Ready 構築の起点となる domain Entity が存在せず、入口 task で
        外部入力 (cron / CLI 引数) から直接構築するため。

        Precondition:
        - ``force=False`` の場合: 同 ``window_end`` の snapshot 未生成
        - ``force=True`` の場合: 既存有無を問わず通す (上書き経路)

        Returns:
            進める場合: ``ReadyForDigest``
            進めない場合: ``None`` (業務正常状態、例外ではない — 既存 snapshot あり
            + force=False。spec §4.5 Failure mode 1)
        """
        candidate = cls(window_end=window_end, force=force)
        if not force and await snapshot_repo.exists_for_window_end(window_end):
            logger.info(
                "digest_skipped_existing_snapshot",
                window_end=window_end.isoformat(),
            )
            return None
        return candidate
