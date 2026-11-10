"""Runのどの試行かを表す値(RunAttempt)が受け付ける実行世代(DBを使わない)。"""

from __future__ import annotations

from uuid import UUID

import pytest

from app.agent.running.types import RunAttempt

_RUN_ID = UUID("00000000-0000-4000-a000-000000000802")


def test_run_attempt_rejects_epoch_of_run_without_started_attempt() -> None:
    """workerがまだ開始していないRunの実行世代0では、試行を表す値を作れない。"""
    with pytest.raises(ValueError, match="positive epoch"):
        RunAttempt(run_id=_RUN_ID, epoch=0)
