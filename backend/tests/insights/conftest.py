"""週次の集約の試験で、標準出力のJSONのログを読むfixture。"""

import json
from collections.abc import Callable

import pytest


@pytest.fixture
def read_logs(capsys) -> Callable[[], list[dict[str, object]]]:
    """出力されたログを、実行ごとに変わる時刻と発生位置を除いて返す。"""

    def _read() -> list[dict[str, object]]:
        logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        for log in logs:
            del log["timestamp"]
            log.pop("frames", None)
        return logs

    return _read
