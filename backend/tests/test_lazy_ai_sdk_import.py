"""遅延 AI SDK import の構造保証テスト (import-time footprint guard)。

非 AI を実行しない taskiq プロセス (scheduler / collect の metadata・content /
maintenance / trend_discovery) は、起動時に重い AI SDK (``openai`` +
``google.genai``、実測 ~133MB) を import してはならない。SDK は AI を実行する
worker の WORKER_STARTUP hook (broker_analysis / broker_embedding /
broker_briefing) でのみロードされる設計 (``app/queue/composition.py`` の遅延 import)。

各プロセスの import surface は ``supervisord/{scheduler,fetch,insights,analysis}.conf``
(maintenance program は analysis.conf) の ``taskiq worker``/``taskiq scheduler`` 起動
引数に一致させる。clean な module table が必要なため subprocess で検証する
(in-process だと他テストが既に SDK を import 済)。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

# supervisord conf の taskiq 起動引数に対応する「非 AI プロセス」の import surface。
# taskiq は `module:object` の module 部 + 各 task module を import するため、ここでは
# import 対象 module のみを列挙する (object 部は import に無関係)。
_NON_AI_IMPORT_SURFACES = {
    # scheduler.conf: taskiq scheduler app.queue.schedulers:<sched> app.queue.registry
    "scheduler": "import app.queue.schedulers, app.queue.registry",
    # fetch.conf: taskiq worker app.queue.brokers:broker_{metadata,content}
    #             app.queue.tasks.acquisition app.queue.tasks.completion
    "collect": (
        "import app.queue.brokers, app.queue.tasks.acquisition, "
        "app.queue.tasks.completion"
    ),
    # analysis.conf (maintenance program): broker_maintenance backfill retention
    "maintenance": (
        "import app.queue.brokers, app.queue.tasks.backfill, app.queue.tasks.retention"
    ),
    # insights.conf (trend program): broker_trend_discovery trend_discovery
    "trend_discovery": "import app.queue.brokers, app.queue.tasks.trend_discovery",
}


def _ai_sdk_modules_loaded_after(import_stmt: str) -> set[str]:
    """clean な interpreter で import_stmt を実行し、ロード済 AI SDK module を返す。"""
    code = textwrap.dedent(
        f"""
        import sys
        {import_stmt}
        loaded = sorted(
            m
            for m in sys.modules
            if m == "openai"
            or m.startswith("openai.")
            or m == "google.genai"
            or m.startswith("google.genai")
        )
        print("\\n".join(loaded))
        """
    )
    result = subprocess.run(  # noqa: S603  (sys.executable + 固定 code、untrusted 入力なし)
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"import surface の subprocess が失敗した:\n{result.stderr}"
    )
    return {line for line in result.stdout.splitlines() if line}


@pytest.mark.parametrize("surface", sorted(_NON_AI_IMPORT_SURFACES))
def test_non_ai_process_import_does_not_load_ai_sdk(surface: str) -> None:
    """非 AI プロセスの import で openai / google.genai がロードされないこと。

    SDK の import-time ロードを構造的に禁じる。回帰すると当該プロセスが待機中も
    ~133MB の AI SDK を常駐させ OOM 余地を作る。
    """
    loaded = _ai_sdk_modules_loaded_after(_NON_AI_IMPORT_SURFACES[surface])
    assert loaded == set(), (
        f"{surface} の import surface が AI SDK をロードした: {sorted(loaded)}"
    )
