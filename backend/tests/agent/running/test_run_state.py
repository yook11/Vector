"""実行世代の読み方(DBを使わない純粋な判定)。"""

from __future__ import annotations

import pytest

from app.agent.running.state import has_attempt_started


@pytest.mark.parametrize(("attempt_epoch", "expected"), [(0, False), (1, True)])
def test_attempt_started_only_after_first_epoch(
    attempt_epoch: int, expected: bool
) -> None:
    """実行世代が0のRunはworkerがまだ開始しておらず、1以上なら開始済みと読む。"""
    assert has_attempt_started(attempt_epoch) is expected
