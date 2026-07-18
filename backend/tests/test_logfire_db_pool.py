"""``app.logfire.db_pool`` の不変条件テスト。"""

from __future__ import annotations

from typing import Any

import logfire
import pytest
from opentelemetry.metrics import CallbackOptions
from structlog.testing import capture_logs

from app.logfire.db_pool import (
    log_pool_initialized,
    pool_stats,
    register_pool_metrics,
)
from app.queue.lifecycle import WORKER_POOL_SIZING, build_worker_engine


class TestLogPoolInitialized:
    """起動時 pool profile ログの構造を検証する。"""

    def test_logs_pool_profile_with_derived_capacity(self) -> None:
        pool_size, max_overflow = 5, 5
        with capture_logs() as logs:
            log_pool_initialized(
                service_name="vector-worker-collection",
                pool_size=pool_size,
                max_overflow=max_overflow,
                pool_recycle=240,
                pool_timeout=5,
            )
        assert logs == [
            {
                "event": "db_pool_initialized",
                "log_level": "info",
                "service": "vector-worker-collection",
                "pool_size": pool_size,
                "max_overflow": max_overflow,
                "capacity": pool_size + max_overflow,
                "pool_recycle": 240,
                "pool_timeout": 5,
            }
        ]


class TestPoolMetrics:
    """observable gauge の登録と pool 値読取の不変条件。"""

    def test_pool_stats_reads_fresh_pool(self) -> None:
        pool_size, _ = WORKER_POOL_SIZING["collection"]
        engine = build_worker_engine("collection")
        assert pool_stats(engine) == {"checked_out": 0, "overflow": -pool_size}

    def test_registers_three_pool_gauges(self, monkeypatch: pytest.MonkeyPatch) -> None:
        registered: list[str] = []

        def _capture(name: str, callbacks: object, **kwargs: object) -> None:
            registered.append(name)

        monkeypatch.setattr(logfire, "metric_gauge_callback", _capture)
        pool_size, max_overflow = WORKER_POOL_SIZING["collection"]
        engine = build_worker_engine("collection")
        register_pool_metrics(engine, pool_size=pool_size, max_overflow=max_overflow)
        assert set(registered) == {
            "vector.db.pool.checked_out",
            "vector.db.pool.overflow",
            "vector.db.pool.capacity",
        }

    def test_pool_gauges_yield_live_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _capture(name: str, callbacks: list[Any], **kwargs: Any) -> None:
            captured[name] = callbacks[0]

        monkeypatch.setattr(logfire, "metric_gauge_callback", _capture)
        pool_size, max_overflow = WORKER_POOL_SIZING["collection"]
        engine = build_worker_engine("collection")
        register_pool_metrics(engine, pool_size=pool_size, max_overflow=max_overflow)

        def _value(name: str) -> int:
            (observation,) = list(captured[name](CallbackOptions()))
            return observation.value

        assert (
            _value("vector.db.pool.checked_out"),
            _value("vector.db.pool.overflow"),
            _value("vector.db.pool.capacity"),
        ) == (0, -pool_size, pool_size + max_overflow)
