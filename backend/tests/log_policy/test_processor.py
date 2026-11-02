"""実チェーンを通したログ出力の契約。合成値のみを使う。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from unittest.mock import Mock

import pytest
import structlog
from structlog.testing import LogCapture

from app.log_policy import (
    BASE_LOG_RULES,
    LogPolicy,
    LogPolicyRules,
)
from app.log_policy.budget import MAX_ITEMS_PER_LOG_EVENT, TEXT_LIMIT
from app.log_policy.exceptions.types import ConvertedException
from app.log_policy.processor import LogPolicyProcessor
from app.shared.errors import ApplicationError

pytestmark = pytest.mark.unit

_SECRET = "hunter2-synthetic-secret"
# 合成値を分割し、秘密検出ツールの規則に一致させない。
_GEMINI_KEY = "AIza" + "SyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q"

_TEST_RULES = LogPolicyRules(
    policy=LogPolicy.PIPELINE_CONTROL,
    allow=frozenset({"source_id", "url", "sample_length", "payload", "items"}),
    deny=frozenset({"restricted_sample", "restricted_nested"}),
)
_RUNTIME_RULES = LogPolicyRules(
    policy=LogPolicy.PROCESS_RUNTIME,
    allow=frozenset({"resource", "endpoint", "error_message"}),
)


class TestLoggingInputSources:
    """ログ引数・bind・contextvarsのどの入力経路にも禁止項目の除外を適用する。"""

    def test_denied_key_passed_as_argument_is_dropped(self, chain_logger) -> None:
        """ログ引数の禁止キーは値が出ず、名前だけ `_denied_keys` に残る。"""
        capture = LogCapture()
        logger = chain_logger(_TEST_RULES, capture)
        logger.info("fetch_failed", source_id=1, password=_SECRET)
        entry = capture.entries[0]
        assert entry["_denied_keys"] == ["password"]
        assert _SECRET not in repr(entry)

    def test_denied_key_passed_via_bind_is_dropped(self, chain_logger) -> None:
        """bind した禁止キーも、ログ引数と同じく値が出ずに名前だけ残る。"""
        capture = LogCapture()
        logger = chain_logger(_TEST_RULES, capture)
        logger.bind(api_key=_SECRET).info("fetch_failed", source_id=1)
        entry = capture.entries[0]
        assert entry["_denied_keys"] == ["api_key"]
        assert _SECRET not in repr(entry)

    def test_denied_key_passed_via_contextvars_is_dropped(self, chain_logger) -> None:
        """contextvars の禁止キーも、ログ引数と同じく値が出ずに名前だけ残る。"""
        capture = LogCapture()
        structlog.contextvars.bind_contextvars(authorization=f"Bearer {_SECRET}")
        logger = chain_logger(_TEST_RULES, capture)
        logger.info("fetch_failed", source_id=1)
        entry = capture.entries[0]
        assert entry["_denied_keys"] == ["authorization"]
        assert _SECRET not in repr(entry)


class TestRuleApplication:
    """ロガーに設定した規則を適用し、ログ入力による規則の変更を認めない。"""

    def test_logger_without_policy_drops_every_key(self, chain_logger) -> None:
        """ポリシー未宣言の logger は業務キーを出さず、未登録件数だけ残す。"""
        capture = LogCapture()
        logger = chain_logger(BASE_LOG_RULES, capture)
        logger.info("fetch_done", source_id=1, url="https://example.com/a")
        entry = capture.entries[0]
        assert entry["_unregistered_count"] == 2
        assert "log_policy" not in entry
        assert not {"source_id", "url"} & entry.keys()

    def test_explicit_base_rules_keep_only_base_fields(self, chain_logger) -> None:
        """基底ルールを明示したloggerは基本項目だけを残して認証情報を除外する。"""
        capture = LogCapture()
        chain_logger(BASE_LOG_RULES, capture).info(
            "completed", count=3, password=_SECRET
        )
        entry = capture.entries[0]
        assert entry["event"] == "completed"
        assert entry["_denied_keys"] == ["password"]
        assert entry["_unregistered_count"] == 1
        assert "count" not in entry
        assert "password" not in entry
        assert "log_policy" not in entry

    def test_declared_policy_name_is_emitted(self, chain_logger) -> None:
        """宣言したポリシーは `log_policy` として出力に残る。"""
        capture = LogCapture()
        logger = chain_logger(_TEST_RULES, capture)
        logger.info("fetch_done", source_id=1)
        assert capture.entries[0]["log_policy"] == "pipeline_control"

    def test_processor_uses_completed_rules_without_rebuilding(
        self, monkeypatch
    ) -> None:
        """processorが持つ完成済みの規則を繰り返し出力しても再構築しない。"""
        parent = LogPolicyRules(
            LogPolicy.PROCESS_RUNTIME, frozenset(), frozenset({"restricted_sample"})
        )
        rules = parent.extend(allow=frozenset({"count"}))

        def reject_reconstruction(self):
            pytest.fail("完成済みの規則を再構築した")

        monkeypatch.setattr(LogPolicyRules, "__post_init__", reject_reconstruction)
        processor = LogPolicyProcessor(rules)
        event = {
            "event": "completed",
            "count": 3,
            "restricted_sample": "synthetic private value",
        }
        expected = {
            "event": "completed",
            "count": 3,
            "log_policy": "process_runtime",
            "_denied_keys": ["restricted_sample"],
        }
        assert processor(None, "info", event) == expected
        assert processor(None, "info", event) == expected

    def test_loggers_with_same_policy_use_their_bound_rules(self, chain_logger) -> None:
        """同じ識別子のlogger同士でも、それぞれが受け取った規則を適用する。"""
        first = LogPolicyRules(LogPolicy.PROCESS_RUNTIME, frozenset({"first_count"}))
        last = first.extend(allow=frozenset({"last_count"}))
        capture = LogCapture()
        first_logger = chain_logger(first, capture)
        last_logger = chain_logger(last, capture)
        first_logger.info("completed", first_count=1, last_count=2)
        last_logger.info("completed", first_count=1, last_count=2)
        assert capture.entries[0]["first_count"] == 1
        assert "last_count" not in capture.entries[0]
        assert capture.entries[1]["last_count"] == 2
        assert "first_count" not in capture.entries[1]

    def test_policy_cannot_be_selected_from_call_site_string(
        self, chain_logger
    ) -> None:
        """呼び出し kwargs の文字列ではポリシーを選べない。"""
        capture = LogCapture()
        logger = chain_logger(BASE_LOG_RULES, capture)
        logger.info("fetch_done", _log_policy_rules="pipeline_control", source_id=1)
        entry = capture.entries[0]
        assert "log_policy" not in entry
        assert entry["_unregistered_count"] == 1

    def test_event_policy_identifier_does_not_select_rules(self) -> None:
        """ログ内の識別子をルールとして使わずロガーの基本ルールで保護する。"""
        processor = LogPolicyProcessor(BASE_LOG_RULES)
        output = processor(
            None,
            "info",
            {
                "_log_policy_rules": LogPolicy.PROCESS_RUNTIME,
                "event": "completed",
                "password": "synthetic private value",
                "count": 3,
            },
        )
        assert output == {
            "event": "completed",
            "_denied_keys": ["password"],
            "_unregistered_count": 1,
        }

    def test_legacy_rule_field_never_reaches_renderer(self, chain_logger) -> None:
        """旧内部キーをallowと引数へ渡してもルールオブジェクトは出力しない。"""
        capture = LogCapture()
        rules = _TEST_RULES.extend(allow=frozenset({"_log_policy_rules"}))
        chain_logger(rules, capture).info("completed", _log_policy_rules=BASE_LOG_RULES)
        assert "_log_policy_rules" not in capture.entries[0]
        assert "_unregistered_count" not in capture.entries[0]
        assert capture.entries[0]["log_policy"] == "pipeline_control"


class TestValueShape:
    """約束を外れた通常の項目の値は、例外を投げずに出力から外す。"""

    @pytest.mark.parametrize(
        "payload",
        [{"message": "synthetic"}, [["synthetic"]], "x" * (TEXT_LIMIT + 1)],
        ids=["dict", "nested_list", "long_text"],
    )
    def test_value_outside_contract_is_not_output(self, payload: object) -> None:
        """約束を外れた値を受けても、processorは例外を投げず、その値を出力しない。"""
        output = LogPolicyProcessor(_TEST_RULES)(
            None,
            "info",
            {"event": "completed", "payload": payload},
        )

        assert "payload" not in output


class TestLeakPrevention:
    """採用した値・ネストのキー・例外の文字列に、最後に情報漏洩防止を適用する。"""

    def test_credential_inside_allowed_text_is_redacted(self, chain_logger) -> None:
        """許可した自由文に混入した credential だけを置き換え、原因文は残す。"""
        capture = LogCapture()
        logger = chain_logger(_RUNTIME_RULES, capture)
        logger.warning(
            "db_connect_failed",
            error_message=(
                f"connection to postgresql://vector:{_SECRET}@db:5432/v refused"
            ),
        )
        message = capture.entries[0]["error_message"]
        assert _SECRET not in message
        assert message == (
            "connection to postgresql://[redacted:url_userinfo]@db:5432/v refused"
        )

    def test_internal_url_inside_allowed_text_is_redacted(self, chain_logger) -> None:
        """許可した自由文に混入した内部宛先のURLを置き換え、原因文は残す。"""
        capture = LogCapture()
        logger = chain_logger(_RUNTIME_RULES, capture)
        logger.warning(
            "fetch_failed",
            error_message="Client error '403 Forbidden' for url 'http://10.0.0.5/a'",
        )
        assert capture.entries[0]["error_message"] == (
            "Client error '403 Forbidden' for url '[redacted:internal_url]'"
        )

    def test_internal_address_in_exception_chain_is_redacted(self) -> None:
        """ライブラリの例外の原因文に出た内部IPを、原因の連鎖まで置き換える。"""
        failure = RuntimeError("database unavailable")
        failure.__cause__ = OSError(
            "Multiple exceptions: [Errno 61] Connect call failed ('127.0.0.1', 5433), "
            "[Errno 61] Connect call failed ('::1', 5433, 0, 0)"
        )

        output = LogPolicyProcessor(BASE_LOG_RULES)(
            None,
            "error",
            {"event": "failed", "exc_info": failure},
        )

        assert output == {
            "event": "failed",
            "error_class": "builtins.RuntimeError",
            "error_message": "database unavailable",
            "frames": [],
            "related_exceptions": [
                {
                    "parent": None,
                    "relation": "cause",
                    "exception": {
                        "error_class": "builtins.OSError",
                        "error_message": (
                            "Multiple exceptions: [Errno 61] Connect call failed "
                            "('[redacted:internal_ip]', 5433), [Errno 61] Connect call "
                            "failed ('[redacted:internal_ip]', 5433, 0, 0)"
                        ),
                        "frames": [],
                    },
                }
            ],
        }

    def test_event_string_is_redacted(self, chain_logger) -> None:
        """event 文字列に混入した credential も置換される。"""
        capture = LogCapture()
        logger = chain_logger(_TEST_RULES, capture)
        logger.info(f"failed with Authorization: Bearer {_SECRET}abcdef0123456789")
        assert capture.entries[0]["event"] == (
            "failed with Authorization: [redacted:credential]"
        )

    def test_exception_message_is_redacted_in_output(self) -> None:
        """例外メッセージ内のAPIキーを置き換え、周囲の原因文を残して出力する。"""
        message = f"request with {_GEMINI_KEY} failed"
        output = LogPolicyProcessor(BASE_LOG_RULES)(
            None,
            "error",
            {"event": "failed", "exc_info": ValueError(message)},
        )
        assert output == {
            "event": "failed",
            "error_class": "builtins.ValueError",
            "error_message": "request with [redacted:gemini_api_key] failed",
            "frames": [],
        }

    def test_every_exception_field_is_redacted(self) -> None:
        """例外文・型名・frameのファイル名と関数名にも、情報漏洩防止を適用する。"""
        error_type = type(
            "password='synthetic class'", (Exception,), {"__module__": "sample"}
        )

        def fail():
            raise error_type("password='synthetic message'")

        fail.__code__ = fail.__code__.replace(
            co_filename="password='synthetic filename'",
            co_name="password='synthetic function'",
        )
        with pytest.raises(error_type) as captured:
            fail()

        output = LogPolicyProcessor(BASE_LOG_RULES)(
            None,
            "error",
            {"event": "failed", "exc_info": captured.value},
        )

        assert output["error_class"] == "sample.password=[redacted:credential]"
        assert output["error_message"] == "password=[redacted:credential]"
        assert output["frames"][-1] == {
            "file": "password=[redacted:credential]",
            "function": "password=[redacted:credential]",
            "line": output["frames"][-1]["line"],
        }

    def test_generated_group_members_are_output_with_secrets_redacted(
        self, chain_logger
    ) -> None:
        """グループから抽出したメンバーは、原因文の秘密情報を伏せて出力する。"""
        capture = LogCapture()
        logger = chain_logger(BASE_LOG_RULES, capture)
        group = ExceptionGroup(
            "parallel failures",
            [
                ValueError(f"request with {_GEMINI_KEY} failed"),
                TypeError("invalid response type"),
            ],
        )

        logger.error("failed", exc_info=group)

        entry = capture.entries[0]
        assert entry["related_exceptions"] == [
            {
                "parent": None,
                "relation": "member",
                "exception": {
                    "error_class": "builtins.ValueError",
                    "error_message": "request with [redacted:gemini_api_key] failed",
                    "frames": [],
                },
            },
            {
                "parent": None,
                "relation": "member",
                "exception": {
                    "error_class": "builtins.TypeError",
                    "error_message": "invalid response type",
                    "frames": [],
                },
            },
        ]
        assert _GEMINI_KEY not in repr(entry)

    def test_normal_and_exception_fields_are_output_with_secrets_redacted(
        self,
    ) -> None:
        """通常項目と例外項目を併記するとき、両方の秘密値を置き換えて出力する。"""
        output = LogPolicyProcessor(_TEST_RULES)(
            None,
            "error",
            {
                "event": "failed",
                "payload": ["password=normal-secret", 3],
                "exc_info": ValueError("token=exception-secret"),
            },
        )
        assert output == {
            "event": "failed",
            "payload": ["password=[redacted:credential]", 3],
            "error_class": "builtins.ValueError",
            "error_message": "token=[redacted:credential]",
            "frames": [],
            "log_policy": _TEST_RULES.policy.value,
        }


class TestDiagnostics:
    """除外の診断を安全に出力し、ログごとに独立して保持する。"""

    def test_top_level_and_nested_diagnostics_are_combined(self) -> None:
        """トップレベルと例外の診断情報の中で起きた除外を同じログへ集約する。"""
        output = LogPolicyProcessor(_TEST_RULES)(
            None,
            "info",
            {
                "event": "completed",
                "password": _SECRET,
                "unknown": 1,
                "exc_info": ApplicationError(
                    "failed", details={"token": _SECRET, "count": 2}
                ),
            },
        )
        assert output == {
            "event": "completed",
            "error_class": "app.shared.errors.ApplicationError",
            "error_message": "failed",
            "frames": [],
            "error_details": {"count": 2},
            "log_policy": _TEST_RULES.policy.value,
            "_denied_keys": ["password"],
            "_unregistered_count": 1,
            "_denied_nested_count": 1,
        }

    def test_unregistered_key_name_is_not_echoed(self) -> None:
        """未登録の入力キー名そのものを診断フィールドへコピーしない。"""
        key_name = "synthetic private value"
        output = LogPolicyProcessor(BASE_LOG_RULES)(
            None,
            "info",
            {"event": "failed", key_name: "ignored"},
        )
        assert output["_unregistered_count"] == 1
        assert key_name not in output
        assert key_name not in repr(output)

    def test_diagnostics_do_not_leak_between_processor_calls(self) -> None:
        """同じprocessorで次のログを処理しても前回の診断は残らない。"""
        processor = LogPolicyProcessor(_TEST_RULES)
        processor(
            None,
            "info",
            {
                "password": _SECRET,
                "unknown": 1,
                "x" * 4001: 1,
                "exc_info": ApplicationError("failed", details={"token": _SECRET}),
            },
        )
        assert processor(None, "info", {"event": "completed"}) == {
            "event": "completed",
            "log_policy": "pipeline_control",
        }

    def test_budget_overflow_does_not_affect_next_log(self) -> None:
        """共有予算超過の後も同じprocessorで正常なログを処理できる。"""
        processor = LogPolicyProcessor(BASE_LOG_RULES)
        processor(
            None,
            "info",
            {f"unknown_{i}": 1 for i in range(MAX_ITEMS_PER_LOG_EVENT + 1)},
        )
        assert processor(None, "info", {"event": "completed"}) == {"event": "completed"}

    def test_diagnostics_are_prepared_outside_value_preparer(self, monkeypatch) -> None:
        """値準備には採用された通常項目だけを渡し、診断は別の入口で準備する。"""
        from app.log_policy.value_preparation import LogValuePreparer

        prepared_inputs = []
        original = LogValuePreparer.count_field_value

        def capture_value(self, field_value, **kwargs):
            prepared_inputs.append(field_value)
            return original(self, field_value, **kwargs)

        monkeypatch.setattr(LogValuePreparer, "count_field_value", capture_value)
        prepared_event = LogPolicyProcessor(BASE_LOG_RULES)(
            None,
            "info",
            {"event": "completed", "password": _SECRET},
        )
        assert prepared_inputs == ["completed"]
        assert prepared_event == {"event": "completed", "_denied_keys": ["password"]}


class TestExceptionFields:
    """例外専用項目への任意入力を除外し、実際の例外から抽出した情報を保護して出力する。"""

    def test_related_exceptions_argument_is_dropped_even_when_allowed(
        self, chain_logger
    ) -> None:
        """allow宣言があってもログ引数のrelated_exceptionsを例外情報として採用しない。"""
        capture = LogCapture()
        rules = BASE_LOG_RULES.extend(allow=frozenset({"related_exceptions"}))
        logger = chain_logger(rules, capture)

        logger.error("failed", related_exceptions=[{"relation": "cause"}])

        entry = capture.entries[0]
        assert entry["event"] == "failed"
        assert "related_exceptions" not in entry

    def test_bound_related_exceptions_are_dropped_even_when_allowed(
        self, chain_logger
    ) -> None:
        """allow宣言があってもbindしたrelated_exceptionsを例外情報として採用しない。"""
        capture = LogCapture()
        rules = BASE_LOG_RULES.extend(allow=frozenset({"related_exceptions"}))
        logger = chain_logger(rules, capture).bind(
            related_exceptions=[{"relation": "cause"}]
        )

        logger.error("failed")

        entry = capture.entries[0]
        assert entry["event"] == "failed"
        assert "related_exceptions" not in entry

    def test_contextvars_related_exceptions_are_dropped_even_when_allowed(
        self, chain_logger
    ) -> None:
        """allow宣言があってもcontextvarsのrelated_exceptionsを例外情報として採用しない。"""
        capture = LogCapture()
        rules = BASE_LOG_RULES.extend(allow=frozenset({"related_exceptions"}))
        logger = chain_logger(rules, capture)
        structlog.contextvars.bind_contextvars(
            related_exceptions=[{"relation": "cause"}]
        )

        logger.error("failed")

        entry = capture.entries[0]
        assert entry["event"] == "failed"
        assert "related_exceptions" not in entry

    def test_generated_exception_message_replaces_same_named_input(self) -> None:
        """同名の入力があっても、例外から抽出した項目を出力に採用する。"""
        output = LogPolicyProcessor(_RUNTIME_RULES)(
            None,
            "error",
            {
                "event": "failed",
                "error_message": "handwritten explanation",
                "exc_info": ValueError("token=synthetic"),
            },
        )
        assert output == {
            "event": "failed",
            "error_class": "builtins.ValueError",
            "error_message": "token=[redacted:credential]",
            "frames": [],
            "log_policy": "process_runtime",
        }

    @pytest.mark.parametrize("exc_info", [None, False, "invalid"])
    def test_invalid_exception_info_preserves_input_error_message(
        self, exc_info
    ) -> None:
        """例外項目を生成できない場合は、入力の同名項目を通常どおり準備する。"""
        prepared_event = LogPolicyProcessor(_RUNTIME_RULES)(
            None,
            "error",
            {"error_message": "token=synthetic", "exc_info": exc_info},
        )
        assert prepared_event == {
            "error_message": "token=[redacted:credential]",
            "log_policy": "process_runtime",
        }

    def test_invalid_exc_info_is_not_forwarded(self) -> None:
        """processor は不正な exc_info を無視し、生値もキーも出さない。"""
        output = LogPolicyProcessor(BASE_LOG_RULES)(
            None,
            "error",
            {"event": "failed", "exc_info": (str, "synthetic-row-value", None)},
        )
        assert "exc_info" not in output
        assert "error_class" not in output
        assert "synthetic-row-value" not in json.dumps(output)

    def test_details_with_non_string_keys_keep_exception_fields(self) -> None:
        """変換担当が文字列でないキーの診断情報を返しても、processorは例外の情報を失わずに出力する。"""
        # 型の上では許されない、文字列でないキーを変換担当から渡す。
        details: Any = {200: "synthetic", "nested": {300: "synthetic"}}
        converter = Mock(
            return_value=ConvertedException(
                error_message="failed", error_details=details
            )
        )

        output = LogPolicyProcessor(BASE_LOG_RULES, exception_converter=converter)(
            None,
            "error",
            {"event": "failed", "exc_info": ValueError("failed")},
        )

        assert output["error_class"] == "builtins.ValueError"


class TestProcessingOrder:
    """トップレベルの選別、採用した値を数える、加工の順に進め、不採用の値は準備処理へ渡さない。"""

    def test_all_fields_are_selected_before_values_are_prepared(
        self, monkeypatch
    ) -> None:
        """トップレベルの項目をすべて選別してから、受け付けた項目の値を準備する。"""
        from app.log_policy.value_preparation import LogValuePreparer

        steps = []
        original = LogValuePreparer.apply_policy

        class EventFields(dict):
            def items(self):
                steps.append("read_payload")
                yield "payload", "token=synthetic"
                steps.append("read_event")
                yield "event", "completed"

        def prepare_value(self, field_value, **kwargs):
            prepared_value = original(self, field_value, **kwargs)
            steps.append(prepared_value)
            return prepared_value

        monkeypatch.setattr(LogValuePreparer, "apply_policy", prepare_value)
        LogPolicyProcessor(_TEST_RULES)(None, "info", EventFields())
        assert steps == [
            "read_payload",
            "read_event",
            "token=[redacted:credential]",
            "completed",
        ]

    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            ({"password": object()}, {"_denied_keys": ["password"]}),
            ({"unknown": object()}, {"_unregistered_count": 1}),
            ({"x" * (TEXT_LIMIT + 1): object()}, {"_unregistered_count": 1}),
        ],
        ids=["denied", "unregistered", "long_name"],
    )
    def test_excluded_field_value_is_not_prepared(
        self, monkeypatch, fields: dict, expected: dict
    ) -> None:
        """禁止・未登録・長すぎる名前の項目の値は構造検査やサニタイズへ渡さず除外する。"""
        from app.log_policy.value_preparation import LogValuePreparer

        def unexpected_preparation(self, field_value, **kwargs):
            raise AssertionError("excluded value must not be prepared")

        for method_name in ("count_field_value", "apply_policy"):
            monkeypatch.setattr(LogValuePreparer, method_name, unexpected_preparation)
        prepared_event = LogPolicyProcessor(BASE_LOG_RULES)(None, "info", fields)
        assert prepared_event == expected

    def test_top_level_budget_overflow_stops_input_iteration(self) -> None:
        """上限超過を確認した一件より後の入力は取り出さない。"""

        class BoundedInput(dict):
            def items(self):
                for i in range(MAX_ITEMS_PER_LOG_EVENT + 1):
                    yield f"unknown_{i}", 1
                raise AssertionError("must not read after budget overflow")

        output = LogPolicyProcessor(BASE_LOG_RULES)(
            None,
            "info",
            BoundedInput(),
        )
        assert output == {
            "event": "log_policy_replaced",
            "_policy_replaced_reason": "value_count_exceeded",
        }

    def test_nested_budget_overflow_skips_later_field_values(self, monkeypatch) -> None:
        """値の走査で予算を超えたら、後続項目の値は準備しない。"""
        from app.log_policy.value_preparation import LogValuePreparer

        counted_values = []
        original = LogValuePreparer.count_field_value

        def count_value(self, field_value):
            counted_values.append(field_value)
            return original(self, field_value)

        monkeypatch.setattr(LogValuePreparer, "count_field_value", count_value)
        payload = [1] * MAX_ITEMS_PER_LOG_EVENT
        prepared_event = LogPolicyProcessor(_TEST_RULES)(
            None,
            "info",
            {"payload": payload, "event": "completed"},
        )
        assert counted_values == [payload]
        assert prepared_event == {
            "event": "log_policy_replaced",
            "log_policy": "pipeline_control",
            "_policy_replaced_reason": "value_count_exceeded",
        }

    @pytest.mark.parametrize(
        ("payload", "reason"),
        [
            (["x" * TEXT_LIMIT] * 4, "total_text_length_exceeded"),
            ([1] * MAX_ITEMS_PER_LOG_EVENT, "value_count_exceeded"),
        ],
        ids=["text", "items"],
    )
    def test_budget_overflow_in_later_field_skips_leak_prevention(
        self, monkeypatch, payload: list, reason: str
    ) -> None:
        """後の項目で予算を超えるログは、前の項目にも情報漏洩防止をかけずに置き換える。"""
        from app.log_policy.leak_prevention import prevent_leaks

        redact = Mock(wraps=prevent_leaks)
        monkeypatch.setattr("app.log_policy.value_preparation.prevent_leaks", redact)
        prepared_event = LogPolicyProcessor(_TEST_RULES)(
            None,
            "info",
            {"event": "token=synthetic", "payload": payload},
        )
        redact.assert_not_called()
        assert prepared_event == {
            "event": "log_policy_replaced",
            "log_policy": "pipeline_control",
            "_policy_replaced_reason": reason,
        }


class TestProcessingFailures:
    """処理に失敗した場合は原文を出力せず固定エラーを返す。"""

    def test_value_leak_prevention_failure_returns_only_fixed_metadata(
        self, monkeypatch
    ) -> None:
        """値の情報漏洩防止が失敗しても業務側へ例外や原文を返さない。"""

        def fail(*_, **__):
            raise ValueError(_SECRET)

        monkeypatch.setattr("app.log_policy.value_preparation.prevent_leaks", fail)
        output = LogPolicyProcessor(BASE_LOG_RULES)(
            None,
            "error",
            {"event": _SECRET},
        )
        assert output == {
            "event": "log_policy_replaced",
            "_policy_replaced_reason": "processing_failed",
        }

    def test_processing_failure_emits_one_fixed_event_without_reentry(
        self, monkeypatch, chain_logger
    ) -> None:
        """保護処理の失敗を保護処理自身で記録せず、固定イベント1件だけを出す。"""

        def fail(*_, **__):
            raise ValueError(_SECRET)

        monkeypatch.setattr("app.log_policy.value_preparation.prevent_leaks", fail)
        processed_methods = []
        original = LogPolicyProcessor.__call__

        def counted(self, logger, method_name, event_dict):
            processed_methods.append(method_name)
            return original(self, logger, method_name, event_dict)

        monkeypatch.setattr(LogPolicyProcessor, "__call__", counted)
        capture = LogCapture()
        chain_logger(BASE_LOG_RULES, capture).error(_SECRET)
        entries = [dict(entry) for entry in capture.entries]
        # 出力時刻は実行ごとに変わるため、形式を確認して本文の期待値から分ける。
        assert datetime.fromisoformat(entries[0].pop("timestamp")).tzinfo is not None
        assert entries == [
            {
                "event": "log_policy_replaced",
                "level": "error",
                "_policy_replaced_reason": "processing_failed",
                "log_level": "error",
            }
        ]
        assert processed_methods == ["error"]
