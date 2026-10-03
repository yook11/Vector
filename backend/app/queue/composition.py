"""AI adapter wiring (Pure DI composition root)。

週次 briefing で利用する AI provider 選択を本 module で hardcode する設計
(Pure DI)。切替は env 変更ではなくコード変更 + worker restart。Stage ごとに別の
抽象を別の具象クラスに紐付けるため、共有 env による誤切替の余地が構造的に生じない。

本 module は配線関数だけを提供する。WORKER_STARTUP への登録と実行順は
``lifecycle.py`` の WorkerRuntime が担う。engine 生成や Logfire bootstrap などの
汎用 lifecycle も ``lifecycle.py`` の責務。

具象 adapter (Gemini SDK) の import は **各関数の本体内に遅延**させる。
本 module は lifecycle 経由で全プロセスが import するため、top-level で具象を import
すると AI を実行しない process (scheduler / collect / trend_discovery)
まで重い SDK (google.genai) を起動時に常駐させてしまう。関数
本体内 import なら、SDK は当該 compose が実際に走る worker (broker_briefing /
broker_agent) でのみロードされる。本契約は
``tests/test_lazy_ai_sdk_import.py`` の import 隔離 oracle で構造的に pin する。
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING

import structlog
from taskiq import TaskiqState

from app.ai_providers.gemini.settings import GeminiConnectionSettings
from app.config import settings

if TYPE_CHECKING:
    from google.genai.client import AsyncClient

logger = structlog.get_logger(__name__)

# タスクの打ち切り(300秒)が先に効くよう、1回の試行の上限はそれより長くする。
_BRIEFING_GEMINI_CONNECTION = GeminiConnectionSettings(read_timeout=600.0)


async def _wire_briefing_adapter(state: TaskiqState) -> None:
    """週次 briefing の LLM generator を worker 起動時に構築する。"""
    from app.insights.briefing.errors import BriefingConfigurationError

    if not settings.gemini_api_key.get_secret_value():
        raise BriefingConfigurationError("GEMINI_API_KEY is not configured")

    # 具象 SDK の import を関数本体に遅延 (module docstring 参照)。
    from app.ai_providers.gemini.client import open_gemini_client
    from app.insights.briefing.llm import GeminiBriefingGenerator

    def open_client() -> AbstractAsyncContextManager[AsyncClient]:
        return open_gemini_client(
            api_key=settings.gemini_api_key,
            settings=_BRIEFING_GEMINI_CONNECTION,
        )

    state.briefing_generator = GeminiBriefingGenerator(client_scope_factory=open_client)
    logger.info(
        "briefing_adapter_wired",
        generator=type(state.briefing_generator).__name__,
        model=state.briefing_generator.MODEL,
    )


async def _prepare_agent_worker(state: TaskiqState) -> None:
    """agent run に必要な設定を確かめ、AI SDK を listener 開始前にロードする。

    設定の欠落は、run を受け取ってから失敗させず worker の起動で止める。
    run 中の遅延 import は 0.25 vCPU の event loop を数秒塞ぎ、listener の
    blocking read が socket_timeout を超えて worker ごと落ちるため、run 経路の
    重い SDK (Gemini) をここで済ませる。
    """
    from app.agent.composition import ensure_agent_worker_configured

    ensure_agent_worker_configured()
    # 具象 SDK の import を関数本体に遅延 (module docstring 参照)。
    import google.genai  # noqa: F401

    logger.info("agent_sdk_imports_warmed")
