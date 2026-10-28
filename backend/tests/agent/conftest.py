"""調査エージェントの試験で、標準出力のJSONのログを読み、書き込みを失敗させるfixture。"""

import json
import sys
from collections.abc import Callable

import pytest


@pytest.fixture
def read_logs(capsys) -> Callable[[], list[dict[str, object]]]:
    """出力されたログを、実行ごとに変わる時刻と、原因の例外を含む発生位置を除いて返す。"""

    def _read() -> list[dict[str, object]]:
        logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        for log in logs:
            del log["timestamp"]
            log.pop("frames", None)
            for related in log.get("related_exceptions", []):
                if isinstance(related["exception"], dict):
                    related["exception"].pop("frames", None)
        return logs

    return _read


@pytest.fixture
def broken_output(monkeypatch):
    """ログの書き込みが毎回失敗する標準出力へ差し替える。"""

    class BrokenOutput:
        def write(self, text):
            raise OSError("log-private")

        def flush(self):
            raise OSError("log-private")

    monkeypatch.setattr(sys, "stdout", BrokenOutput())
