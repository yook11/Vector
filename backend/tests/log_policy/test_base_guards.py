"""予約キー・独自型・サイズ上限・処理失敗では原文へ戻らない。"""

from __future__ import annotations

import json

import pytest
import structlog

from app.log_policy import LogPolicy, LogPolicyRules
from app.log_policy.processor import LogPolicyProcessor

pytestmark = pytest.mark.unit

_SECRET = "synthetic private value"
_RULES = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({"payload"}))


class TestReservedFields:
    """予約名は保護の迂回にならず、生スタックや非文字列の予約値は出さない。"""

    def test_structured_event_is_replaced_with_unsupported_marker(self) -> None:
        """予約フィールドに構造化した値を渡しても素通しせず、固定マーカーにする。"""
        output = LogPolicyProcessor()(None, "info", {"event": {"content": _SECRET}})
        assert output["event"] == "[unsupported]"
        assert _SECRET not in json.dumps(output)

    @pytest.mark.parametrize("key", ["stack", "stack_info", "exception", "_record"])
    def test_raw_traceback_and_record_are_not_forwarded(self, key: str) -> None:
        """生スタックや LogRecord を renderer に渡さない。"""
        output = LogPolicyProcessor()(None, "info", {"event": "failed", key: _SECRET})
        assert key not in output
        assert output["event"] == "failed"
        assert _SECRET not in json.dumps(output)

    def test_allowed_exception_key_is_still_dropped(self) -> None:
        """目的ポリシーで `exception` を許可しても予約キーとしては出さない。"""
        rules = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({"exception"}))
        output = LogPolicyProcessor([rules])(
            None,
            "error",
            {
                "event": "failed",
                "_log_policy": LogPolicy.INFRASTRUCTURE,
                "exception": _SECRET,
            },
        )
        assert "exception" not in output
        assert output["event"] == "failed"
        assert _SECRET not in json.dumps(output)

    def test_reserved_level_with_invalid_type_does_not_break_console(self) -> None:
        """予約フィールドの不正な型は固定マーカーにし、console を落とさない。"""
        output = LogPolicyProcessor()(
            None, "info", {"event": "failed", "level": {"content": _SECRET}}
        )
        assert output["event"] == "failed"
        assert output["level"] == "[unsupported]"
        rendered = structlog.dev.ConsoleRenderer(colors=False)(None, "info", output)
        assert "failed" in rendered
        assert _SECRET not in rendered


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
            "_log_policy": LogPolicy.INFRASTRUCTURE,
            "payload": {"object": Unsafe(), "bytes": _SECRET.encode()},
        }
        output = LogPolicyProcessor([_RULES])(None, "error", event)
        rendered = renderer(None, "error", output)
        assert _SECRET not in rendered
        assert "[unsupported]" in rendered


class TestValueBounds:
    """循環・深さ・件数の上限では固定マーカーを出し、安全な兄弟値・先頭側は残す。"""

    def test_cyclic_value_is_bounded_and_siblings_survive(self) -> None:
        """循環は `[cycle]` にし、同じオブジェクトの安全な兄弟値は残す。"""
        payload = {"password": _SECRET, "attempt": 2}
        payload["cycle"] = payload
        output = LogPolicyProcessor([_RULES])(
            None,
            "info",
            {
                "event": "failed",
                "_log_policy": LogPolicy.INFRASTRUCTURE,
                "payload": payload,
            },
        )
        assert output["payload"] == {"attempt": 2, "cycle": "[cycle]"}
        assert _SECRET not in json.dumps(output)

    def test_shared_value_is_not_mistaken_for_cycle(self) -> None:
        """循環していない同一オブジェクトの参照はそれぞれ保護して出す。"""
        shared = {"attempt": 2, "password": _SECRET}
        output = LogPolicyProcessor([_RULES])(
            None,
            "info",
            {
                "event": "failed",
                "_log_policy": LogPolicy.INFRASTRUCTURE,
                "payload": [shared, shared],
            },
        )
        assert output["payload"] == [{"attempt": 2}, {"attempt": 2}]

    def test_deep_value_is_bounded(self) -> None:
        """深い入れ子は `[limit]` で打ち切り、最深の原文は出さない。"""
        payload: dict = {"password": _SECRET}
        for _ in range(100):
            payload = {"nested": payload}
        output = LogPolicyProcessor([_RULES])(
            None,
            "info",
            {
                "event": "failed",
                "_log_policy": LogPolicy.INFRASTRUCTURE,
                "payload": payload,
            },
        )
        node: object = output["payload"]
        for _ in range(20):
            if node == "[limit]":
                break
            assert isinstance(node, dict)
            node = node["nested"]
        else:
            raise AssertionError("depth limit was not applied")
        assert _SECRET not in json.dumps(output)

    def test_wide_value_is_bounded(self) -> None:
        """大量の項目は先頭側を残して `[limit]` で打ち切る。"""
        items = list(range(2000))
        output = LogPolicyProcessor([_RULES])(
            None,
            "info",
            {
                "event": "failed",
                "_log_policy": LogPolicy.INFRASTRUCTURE,
                "payload": items,
            },
        )
        payload = output["payload"]
        assert payload[0] == 0
        assert payload[-1] == "[limit]"
        assert payload[:-1] == items[: len(payload) - 1]
        assert len(payload) <= 1000


class TestProcessorFailure:
    """保護処理の失敗は原文 fallback せず、固定イベントだけ返す。"""

    def test_processor_failure_returns_only_fixed_metadata(self, monkeypatch) -> None:
        """保護処理自体が失敗しても業務側へ例外や原文を返さない。"""

        def fail(*_):
            raise ValueError(_SECRET)

        monkeypatch.setattr("app.log_policy.value_protection.sanitize_text", fail)
        output = LogPolicyProcessor()(None, "error", {"event": _SECRET})
        assert output == {
            "event": "log_policy_failed",
            "_policy_error": "processing_failed",
        }
