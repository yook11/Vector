"""記事単位AI分析で共有する目的ポリシー付きロガーの構築口。"""

from structlog.typing import FilteringBoundLogger

from app.log_policy.policies.ai_inference import AI_INFERENCE_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger


def create_article_analysis_logger() -> FilteringBoundLogger:
    """他の工程のグローバル設定を変更せず、呼び出し専用の出力先へ接続する。"""
    return create_policy_json_logger("article_analysis", AI_INFERENCE_LOG_RULES).bind(
        service="article_analysis"
    )
