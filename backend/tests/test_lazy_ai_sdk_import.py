"""遅延 AI SDK import の構造保証テスト (import-time footprint guard)。

非 AI を実行しない taskiq プロセス (scheduler / collect の dispatch・collection /
maintenance / trend_discovery) と API プロセスの module import は、起動時に重い
AI SDK (``openai`` + ``google.genai``、実測 ~133MB) を import してはならない。
SDK は AI を実行する worker の WORKER_STARTUP hook (broker_analysis /
broker_embedding / broker_briefing)、または API の request-scoped factory 内でのみ
ロードされる設計 (``app/queue/composition.py`` と ``app/agent/router.py`` の遅延
import)。

各プロセスの import surface は ``supervisord/{scheduler,fetch,insights,analysis}.conf``
(maintenance program は analysis.conf) の ``taskiq worker``/``taskiq scheduler`` 起動
引数に一致させる。clean な module table が必要なため subprocess で検証する
(in-process だと他テストが既に SDK を import 済)。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap

import pytest

# supervisord conf の taskiq 起動引数に対応する「非 AI プロセス」の import surface。
# taskiq は `module:object` の module 部 + 各 task module を import するため、ここでは
# import 対象 module のみを列挙する (object 部は import に無関係)。
_NON_AI_IMPORT_SURFACES = {
    # API process: FastAPI app import + route registration は SDK-free に保つ。
    "api": "import app.main",
    # API route / schema import で app.agent package が読まれても SDK-free に保つ。
    "agent_package": "import app.agent",
    # scheduler.conf: python -m app.queue.scheduler_entrypoint (5 cron scheduler 統合)。
    # entrypoint は schedulers + registry を import するため最広の import surface。
    "scheduler": "import app.queue.scheduler_entrypoint",
    # fetch.conf: taskiq worker app.queue.brokers:broker_{dispatch,collection}
    #             app.queue.tasks.acquisition app.queue.tasks.completion
    "collect": (
        "import app.queue.brokers, app.queue.tasks.acquisition, "
        "app.queue.tasks.completion"
    ),
    # analysis.conf (maintenance program): broker_maintenance backfill retention
    "maintenance": (
        "import app.queue.brokers, app.queue.tasks.backfill, "
        "app.queue.tasks.retention, app.queue.tasks.queue_health"
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
    if (
        surface == "maintenance"
        and importlib.util.find_spec("app.queue.tasks.queue_health") is None
    ):
        pytest.fail("app.queue.tasks.queue_health is not implemented")

    loaded = _ai_sdk_modules_loaded_after(_NON_AI_IMPORT_SURFACES[surface])
    assert loaded == set(), (
        f"{surface} の import surface が AI SDK をロードした: {sorted(loaded)}"
    )


def test_planner_runtime_scope_construction_keeps_provider_imports_lazy() -> None:
    """Planner scopeはenterされるまでGemini具象RuntimeとSDKをloadしない。"""
    code = textwrap.dedent(
        """
        import sys
        from app.agent.composition import activate_planner_runtime

        activate_planner_runtime()
        forbidden = sorted(
            module
            for module in sys.modules
            if module == "app.agent.runtime.gemini"
            or module == "google.genai"
            or module.startswith("google.genai.")
        )
        print("\\n".join(forbidden))
        """
    )
    result = subprocess.run(  # noqa: S603  (sys.executable + 固定code)
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
