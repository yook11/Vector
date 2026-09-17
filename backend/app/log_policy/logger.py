"""適用するポリシーを宣言した logger の構築口。"""

from __future__ import annotations

import structlog
from structlog.typing import FilteringBoundLogger

from app.log_policy.rules import LogPolicy
from app.log_policy.selection import POLICY_BIND_KEY


def policy_logger(name: str, policy: LogPolicy) -> FilteringBoundLogger:
    """ポリシー識別子を initial_values に持つ lazy logger を返す。

    ``get_logger(name).bind(...)`` だと import 時点の structlog 設定で実体化され、
    その後の ``structlog.configure`` が効かないため、initial_values で渡す。
    """
    return structlog.get_logger(name, **{POLICY_BIND_KEY: policy})
