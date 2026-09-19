"""適用するポリシーを宣言した logger の構築口。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog
from structlog.typing import FilteringBoundLogger

from app.log_policy.base import BASE_LOG_RULES, LogPolicyRules


@dataclass(frozen=True)
class PolicyLogger:
    """ログ内容と分離したルールを保持し、整形済みのログを出力先へ渡す。"""

    rules: LogPolicyRules
    _output_logger: Any

    def __getattr__(self, name: str) -> Any:
        return getattr(self._output_logger, name)


def create_policy_logger(
    name: str | None = None,
    rules: LogPolicyRules = BASE_LOG_RULES,
    *,
    output_logger_factory: Callable[..., Any],
) -> PolicyLogger:
    """structlogの実体化時に、確定済みルールを持つ出力先ロガーを作る。"""
    if type(rules) is not LogPolicyRules:
        raise TypeError("rules must be LogPolicyRules")
    return PolicyLogger(
        rules=rules,
        _output_logger=output_logger_factory(name),
    )


def policy_logger(
    name: str, rules: LogPolicyRules = BASE_LOG_RULES
) -> FilteringBoundLogger:
    """ルールを生成引数として預け、共通設定で実体化する遅延ロガーを返す。"""
    return structlog.wrap_logger(None, logger_factory_args=(name, rules))
