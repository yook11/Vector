"""AI adapter wiring (Pure DI composition root)。

Stage 3 (curation) / Stage 4 (assessment) / Stage 5 (embedding) で利用する AI
provider 選択を本 module で hardcode する設計 (Pure DI)。切替は env 変更ではなく
コード変更 + worker restart。Stage ごとに別の抽象を別の具象クラスに紐付けるため、
共有 env による誤切替の余地が構造的に生じない。

本 module は import するだけで broker_analysis / broker_embedding の
WORKER_STARTUP に AI adapter 構築 hook を attach する (副作用)。engine 生成や
Logfire bootstrap などの汎用 lifecycle は ``lifecycle.py`` の責務、本 module は
AI provider 配線に純化する。
"""

from __future__ import annotations

import structlog
from taskiq import TaskiqEvents, TaskiqState

from app.analysis.assessment.ai.deepseek import DeepSeekAssessor
from app.analysis.curation.ai.gemini import GeminiCurator
from app.analysis.embedding.ai.gemini import GeminiEmbedder
from app.analysis.rate_limit import ProviderRateLimitGate
from app.queue.brokers import broker_analysis, broker_embedding

logger = structlog.get_logger(__name__)


@broker_analysis.on_event(TaskiqEvents.WORKER_STARTUP)
async def _wire_analysis_adapters(state: TaskiqState) -> None:
    """Stage 3 / Stage 4 の AI アダプターを worker 起動時に構築する。"""
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
    state.embedder = GeminiEmbedder()
    state.provider_rate_limit_gate = ProviderRateLimitGate()
    logger.info(
        "embedding_adapters_wired",
        embedder=type(state.embedder).__name__,
        embedder_model=state.embedder.model_name,
    )
