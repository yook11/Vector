"""独自型・循環参照では原文へ戻らない。"""

from __future__ import annotations

import json

import pytest
import structlog

from app.log_policy import LogPolicy, LogPolicyRules, PolicyLogger
from app.log_policy.processor import LogPolicyProcessor

pytestmark = pytest.mark.unit

_SECRET = "synthetic private value"
_RULES = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({"payload"}))


class TestUnsupportedValues:
    """独自オブジェクトの repr / __structlog__ は呼ばず、renderer に渡さない。"""

    @pytest.mark.parametrize(
        "renderer",
        [
            structlog.processors.JSONRenderer(),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
    )
    def test_arbitrary_objects_never_reach_renderer(self, renderer) -> None:
        """独自オブジェクトの repr や structlog フックを呼ばずに除外する。"""

        class Unsafe:
            def __repr__(self):
                raise AssertionError("repr must not run")

            def __structlog__(self):
                raise AssertionError("serialization hook must not run")

        event = {
            "event": "failed",
            "payload": {"object": Unsafe(), "bytes": _SECRET.encode()},
        }
        output = LogPolicyProcessor()(
            PolicyLogger(_RULES, structlog.ReturnLogger()), "error", event
        )
        rendered = renderer(None, "error", output)
        assert _SECRET not in rendered
        assert "[unsupported]" in rendered


class TestCyclicReferences:
    """循環はその位置だけ置換し、循環していない共有参照は保護して出す。"""

    def test_cyclic_value_is_bounded_and_siblings_survive(self) -> None:
        """循環は `[cycle]` にし、同じオブジェクトの安全な兄弟値は残す。"""
        payload = {"password": _SECRET, "attempt": 2}
        payload["cycle"] = payload
        output = LogPolicyProcessor()(
            PolicyLogger(_RULES, structlog.ReturnLogger()),
            "info",
            {
                "event": "failed",
                "payload": payload,
            },
        )
        assert output["payload"] == {"attempt": 2, "cycle": "[cycle]"}
        assert _SECRET not in json.dumps(output)

    def test_shared_value_is_not_mistaken_for_cycle(self) -> None:
        """循環していない同一オブジェクトの参照はそれぞれ保護して出す。"""
        shared = {"attempt": 2, "password": _SECRET}
        output = LogPolicyProcessor()(
            PolicyLogger(_RULES, structlog.ReturnLogger()),
            "info",
            {
                "event": "failed",
                "payload": [shared, shared],
            },
        )
        assert output["payload"] == [{"attempt": 2}, {"attempt": 2}]
