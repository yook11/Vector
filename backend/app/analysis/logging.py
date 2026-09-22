"""記事単位AI分析で共有する目的ポリシー付きロガーの構築口。"""

import structlog
from structlog.typing import FilteringBoundLogger

from app.log_policy import build_processors, create_policy_logger
from app.log_policy.bound_logger import ApplicationBoundLogger
from app.log_policy.policies.ai_inference import AI_INFERENCE_LOG_RULES


def create_article_analysis_logger() -> FilteringBoundLogger:
    """他の工程のグローバル設定を変更せず、呼び出し専用の出力先へ接続する。"""
    return structlog.wrap_logger(
        create_policy_logger(
            "article_analysis",
            AI_INFERENCE_LOG_RULES,
            output_logger_factory=structlog.WriteLoggerFactory(),
        ),
        processors=build_processors(structlog.processors.JSONRenderer()),
        wrapper_class=ApplicationBoundLogger,
        context_class=dict,
        cache_logger_on_first_use=False,
    ).bind(service="article_analysis")
