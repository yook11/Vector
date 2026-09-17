"""ログ基底ポリシーのテスト用に、実チェーンを通した出力を捕捉する fixture。

``structlog.testing.capture_logs`` は configured processors を差し替えるため、
ポリシー processor を通した結果を見るには実チェーン + ``LogCapture`` が必要。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator

import pytest
import structlog
from structlog.testing import LogCapture

from app.log_policy import LogPolicyRules, build_processors

pytestmark = pytest.mark.unit


@pytest.fixture
def configure_chain() -> Iterator[Callable[[Iterable[LogPolicyRules]], LogCapture]]:
    """与えた目的ポリシーで実チェーンを構成し、捕捉した entries を返す。"""
    original = structlog.get_config().copy()

    def _configure(rules: Iterable[LogPolicyRules]) -> LogCapture:
        capture = LogCapture()
        structlog.configure(
            processors=build_processors(rules, capture),
            wrapper_class=structlog.make_filtering_bound_logger(0),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=False,
        )
        return capture

    yield _configure
    structlog.contextvars.clear_contextvars()
    structlog.configure(**original)
