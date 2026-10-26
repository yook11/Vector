"""目的別ルールをグローバル設定から独立したJSON出力へ接続する。"""

import structlog
from structlog.typing import FilteringBoundLogger

from app.log_policy.base import LogPolicyRules
from app.log_policy.bound_logger import ApplicationBoundLogger
from app.log_policy.chain import build_processors
from app.log_policy.logger import create_policy_logger


def create_policy_json_logger(name: str, rules: LogPolicyRules) -> FilteringBoundLogger:
    """書き込み時の標準出力へ出すので、モジュールで一度作って使い回せる。"""
    return structlog.wrap_logger(
        create_policy_logger(
            name,
            rules,
            output_logger_factory=structlog.PrintLoggerFactory(),
        ),
        processors=build_processors(structlog.processors.JSONRenderer()),
        wrapper_class=ApplicationBoundLogger,
        context_class=dict,
        cache_logger_on_first_use=False,
    ).bind()
