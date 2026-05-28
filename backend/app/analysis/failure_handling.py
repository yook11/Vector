"""analysis task に返す失敗後処理 decision。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FailureHandlingDecision:
    """Task 層が解釈する失敗後処理の結果。

    ``reraise`` は taskiq retry へ渡すかどうか、``stage_hold_reason`` は
    backfill の stage hold を立てる理由を表す。Redis 等の task orchestration
    side effect は caller が実行する。
    """

    reraise: bool
    stage_hold_reason: str | None = None
