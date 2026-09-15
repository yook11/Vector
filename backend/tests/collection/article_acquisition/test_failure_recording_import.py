"""取得の失敗監査をタスクキューなしで利用できることを確認する。"""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_acquisition_failure_recording_loads_without_task_queue_or_settings() -> None:
    """取得入口とRecorderはRedis・Taskiq・全体設定を読み込まない。"""
    code = """
import importlib.abc
import sys

forbidden = ("redis", "taskiq", "taskiq_redis", "app.queue", "app.config")

class RejectQueueDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if any(
            fullname == name or fullname.startswith(name + ".") for name in forbidden
        ):
            raise AssertionError("acquisition loaded " + fullname)

sys.meta_path.insert(0, RejectQueueDependencies())
from app.collection.article_acquisition.failure_recording import (
    ArticleAcquisitionFailureRecorder,
)
from app.lambda_handlers.acquisition.handler import handler

assert callable(handler)
assert not any(
    module == name or module.startswith(name + ".")
    for module in sys.modules
    for name in forbidden
)
"""
    result = subprocess.run(  # noqa: S603 — 固定した検証コードだけを実行する。
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[3],
        env={},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
