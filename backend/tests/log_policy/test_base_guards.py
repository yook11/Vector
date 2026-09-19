"""予約キー・独自型・循環参照では原文へ戻らない。"""

from __future__ import annotations

import json

import pytest
import structlog

from app.log_policy import BASE_LOG_RULES, LogPolicy, LogPolicyRules, PolicyLogger
from app.log_policy.processor import LogPolicyProcessor

pytestmark = pytest.mark.unit

_SECRET = "synthetic private value"
_RULES = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({"payload"}))


class TestReservedFields:
    """予約名でもdenyを適用し、生スタックはrendererへ渡さない。"""

    def test_structured_event_applies_policy_deny(self) -> None:
        """構造化したeventの内部にも目的別のdenyを適用する。"""
        rules = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE, frozenset(), deny=frozenset({"content"})
        )
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {"event": {"content": _SECRET, "count": 1}},
        )
        assert output["event"] == {"count": 1}
        assert _SECRET not in json.dumps(output)

    @pytest.mark.parametrize("key", ["stack", "stack_info", "exception", "_record"])
    def test_raw_traceback_and_record_are_not_forwarded(self, key: str) -> None:
        """生スタックや LogRecord を renderer に渡さない。"""
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": "failed", key: _SECRET},
        )
        assert key not in output
        assert output["event"] == "failed"
        assert _SECRET not in json.dumps(output)

    def test_allowed_exception_key_is_still_dropped(self) -> None:
        """目的ポリシーで `exception` を許可しても予約キーとしては出さない。"""
        rules = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({"exception"}))
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "error",
            {
                "event": "failed",
                "exception": _SECRET,
            },
        )
        assert "exception" not in output
        assert output["event"] == "failed"
        assert _SECRET not in json.dumps(output)


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
