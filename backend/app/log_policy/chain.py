"""アプリログ用 structlog processor チェーンの構成。Logfire とは独立。"""

from __future__ import annotations

from collections.abc import Iterable

import structlog
from structlog.types import Processor

from app.log_policy.processor import LogPolicyProcessor
from app.log_policy.rules import LogPolicyRules


def build_processors(
    rules: Iterable[LogPolicyRules], renderer: Processor
) -> list[Processor]:
    """ポリシー processor を renderer の直前に置いたチェーンを返す。"""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        LogPolicyProcessor(rules),
        renderer,
    ]
