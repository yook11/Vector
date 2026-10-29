"""ログ基底ポリシーのテスト用に、実チェーンを通した出力を捕捉する fixture。

``structlog.testing.capture_logs`` は configured processors を差し替えるため、
ポリシー processor を通した結果を見るには実チェーン + ``LogCapture`` が必要。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
import structlog
from structlog.typing import FilteringBoundLogger, Processor

from app.log_policy import LogPolicyRules, build_processors

pytestmark = pytest.mark.unit


@pytest.fixture
def chain_logger() -> Iterator[
    Callable[[LogPolicyRules, Processor], FilteringBoundLogger]
]:
    """実チェーンの末尾に renderer を置いたロガーを、規則ごとに作る。"""

    def _create(rules: LogPolicyRules, renderer: Processor) -> FilteringBoundLogger:
        return structlog.wrap_logger(
            structlog.ReturnLogger(),
            processors=build_processors(rules, renderer),
            wrapper_class=structlog.make_filtering_bound_logger(0),
            context_class=dict,
        ).bind()

    yield _create
    structlog.contextvars.clear_contextvars()
