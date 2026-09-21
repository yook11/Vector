"""アプリログ用 structlog processor チェーンの構成。Logfire とは独立。"""

from __future__ import annotations

import structlog
from structlog.types import Processor

from app.log_policy.exceptions.conversion import ExceptionConverter, convert_exception
from app.log_policy.processor import LogPolicyProcessor


def build_processors(
    renderer: Processor,
    *,
    exception_converter: ExceptionConverter = convert_exception,
) -> list[Processor]:
    """ポリシー processor を renderer の直前に置いたチェーンを返す。"""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        LogPolicyProcessor(exception_converter=exception_converter),
        renderer,
    ]
