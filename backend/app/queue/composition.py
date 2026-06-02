"""AI adapter wiring (Pure DI composition root)。

Stage 3 (curation) / Stage 4 (assessment) / Stage 5 (embedding) と週次 briefing で
利用する AI provider 選択を本 module で hardcode する設計 (Pure DI)。切替は env 変更
ではなくコード変更 + worker restart。Stage ごとに別の抽象を別の具象クラスに紐付ける
ため、共有 env による誤切替の余地が構造的に生じない。

本 module は import するだけで broker_analysis / broker_embedding / broker_briefing の
WORKER_STARTUP に AI adapter 構築 hook を attach する (副作用)。engine 生成や
Logfire bootstrap などの汎用 lifecycle は ``lifecycle.py`` の責務、本 module は
AI provider 配線に純化する。

具象 adapter (Gemini / DeepSeek SDK) の import は **各 hook の本体内に遅延**させる。
本 module は brokers.py から全プロセスが import するため、top-level で具象を import
すると AI を実行しない process (scheduler / collect / maintenance / trend_discovery)
まで重い SDK (openai + google.genai、実測 ~133MB) を起動時に常駐させてしまう。hook
本体内 import なら、SDK は当該 hook が実際に走る worker (broker_analysis /
broker_embedding / broker_briefing) でのみロードされる。本契約は
``tests/test_lazy_ai_sdk_import.py`` の import 隔離 oracle で構造的に pin する。
"""

from __future__ import annotations

import structlog
from taskiq import TaskiqEvents, TaskiqState

from app.analysis.rate_limit import ProviderRateLimitGate
from app.queue.brokers import broker_analysis, broker_briefing, broker_embedding

logger = structlog.get_logger(__name__)


@broker_analysis.on_event(TaskiqEvents.WORKER_STARTUP)
async def _wire_analysis_adapters(state: TaskiqState) -> None:
    """Stage 3 / Stage 4 の AI アダプターを worker 起動時に構築する。"""
    # 具象 SDK の import を hook 本体に遅延 (module docstring 参照)。
    from app.analysis.assessment.ai.deepseek import DeepSeekAssessor
    from app.analysis.curation.ai.gemini import GeminiCurator

    state.curator = GeminiCurator()
    state.assessor = DeepSeekAssessor()
    state.provider_rate_limit_gate = ProviderRateLimitGate()
    logger.info(
        "analysis_adapters_wired",
        curator=type(state.curator).__name__,
        curator_model=state.curator.model_name,
        assessor=type(state.assessor).__name__,
        assessor_model=state.assessor.model_name,
    )


@broker_embedding.on_event(TaskiqEvents.WORKER_STARTUP)
async def _wire_embedding_adapters(state: TaskiqState) -> None:
    """Stage 5 の embedder アダプターを worker 起動時に構築する。"""
    # 具象 SDK の import を hook 本体に遅延 (module docstring 参照)。
    from app.analysis.embedding.ai.gemini import GeminiEmbedder

    state.embedder = GeminiEmbedder()
    state.provider_rate_limit_gate = ProviderRateLimitGate()
    logger.info(
        "embedding_adapters_wired",
        embedder=type(state.embedder).__name__,
        embedder_model=state.embedder.model_name,
    )


@broker_briefing.on_event(TaskiqEvents.WORKER_STARTUP)
async def _wire_briefing_adapter(state: TaskiqState) -> None:
    """週次 briefing の LLM generator を worker 起動時に構築する。"""
    # 具象 SDK の import を hook 本体に遅延 (module docstring 参照)。
    from app.insights.briefing.llm.deepseek import DeepSeekBriefingGenerator

    state.briefing_generator = DeepSeekBriefingGenerator()
    logger.info(
        "briefing_adapter_wired",
        generator=type(state.briefing_generator).__name__,
        model=state.briefing_generator.MODEL,
    )
