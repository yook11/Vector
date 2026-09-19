"""AI adapter wiring (Pure DI composition root)。

週次 briefing で利用する AI provider 選択を本 module で hardcode する設計
(Pure DI)。切替は env 変更ではなくコード変更 + worker restart。Stage ごとに別の
抽象を別の具象クラスに紐付けるため、共有 env による誤切替の余地が構造的に生じない。

本 module は配線関数だけを提供する。WORKER_STARTUP への登録と実行順は
``lifecycle.py`` の WorkerRuntime が担う。engine 生成や Logfire bootstrap などの
汎用 lifecycle も ``lifecycle.py`` の責務。

具象 adapter (Gemini / DeepSeek SDK) の import は **各関数の本体内に遅延**させる。
本 module は lifecycle 経由で全プロセスが import するため、top-level で具象を import
すると AI を実行しない process (scheduler / collect / trend_discovery)
まで重い SDK (openai + google.genai、実測 ~133MB) を起動時に常駐させてしまう。関数
本体内 import なら、SDK は当該 compose が実際に走る worker (broker_briefing /
broker_agent) でのみロードされる。本契約は
``tests/test_lazy_ai_sdk_import.py`` の import 隔離 oracle で構造的に pin する。
"""

from __future__ import annotations

import structlog
from taskiq import TaskiqState

logger = structlog.get_logger(__name__)


async def _wire_briefing_adapter(state: TaskiqState) -> None:
    """週次 briefing の LLM generator を worker 起動時に構築する。"""
    # 具象 SDK の import を関数本体に遅延 (module docstring 参照)。
    from app.insights.briefing.llm import DeepSeekBriefingGenerator

    state.briefing_generator = DeepSeekBriefingGenerator()
    logger.info(
        "briefing_adapter_wired",
        generator=type(state.briefing_generator).__name__,
        model=state.briefing_generator.MODEL,
    )


async def _warm_agent_sdk_imports(state: TaskiqState) -> None:
    """agent run が使う AI SDK を listener 開始前にロードする。

    run 中の遅延 import は 0.25 vCPU の event loop を数秒塞ぎ、listener の
    blocking read が socket_timeout を超えて worker ごと落ちるため、run 経路の
    重い SDK (Gemini / OpenAI 互換 client) をここで済ませる。
    """
    # 具象 SDK の import を関数本体に遅延 (module docstring 参照)。
    import google.genai  # noqa: F401
    import openai  # noqa: F401

    logger.info("agent_sdk_imports_warmed")
