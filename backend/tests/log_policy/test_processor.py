"""実チェーンを通したログ出力の契約。合成値のみを使う。"""

from __future__ import annotations

import pytest
import structlog

from app.log_policy import (
    BASE_LOG_RULES,
    LogPolicy,
    LogPolicyRules,
    PolicyLogger,
    policy_logger,
)
from app.log_policy.budget import MAX_ITEMS_PER_LOG_EVENT, TEXT_LIMIT
from app.log_policy.processor import LogPolicyProcessor

pytestmark = pytest.mark.unit

_SECRET = "hunter2-synthetic-secret"
_PRIVATE_SAMPLE = "保護対象の合成テキスト"

_TEST_RULES = LogPolicyRules(
    policy=LogPolicy.PIPELINE_CONTROL,
    allow=frozenset({"source_id", "url", "sample_length", "payload", "items"}),
    deny=frozenset({"restricted_sample", "restricted_nested"}),
)
_INFRA_RULES = LogPolicyRules(
    policy=LogPolicy.INFRASTRUCTURE,
    allow=frozenset({"resource", "endpoint", "error_message"}),
)


class TestLoggingInputSources:
    """ログ引数・bind・contextvarsのどの入力経路にも禁止項目の除外を適用する。"""

    def test_denied_key_passed_as_argument_is_dropped(self, configure_chain) -> None:
        """ログ引数の禁止キーは値が出ず、名前だけ `_denied_keys` に残る。"""
        capture = configure_chain()
        logger = policy_logger("test", _TEST_RULES)
        logger.info("fetch_failed", source_id=1, password=_SECRET)
        entry = capture.entries[0]
        assert entry["_denied_keys"] == ["password"]
        assert _SECRET not in repr(entry)

    def test_denied_key_passed_via_bind_is_dropped(self, configure_chain) -> None:
        """bind した禁止キーも、ログ引数と同じく値が出ずに名前だけ残る。"""
        capture = configure_chain()
        logger = policy_logger("test", _TEST_RULES)
        logger.bind(api_key=_SECRET).info("fetch_failed", source_id=1)
        entry = capture.entries[0]
        assert entry["_denied_keys"] == ["api_key"]
        assert _SECRET not in repr(entry)

    def test_denied_key_passed_via_contextvars_is_dropped(
        self, configure_chain
    ) -> None:
        """contextvars の禁止キーも、ログ引数と同じく値が出ずに名前だけ残る。"""
        capture = configure_chain()
        structlog.contextvars.bind_contextvars(authorization=f"Bearer {_SECRET}")
        logger = policy_logger("test", _TEST_RULES)
        logger.info("fetch_failed", source_id=1)
        entry = capture.entries[0]
        assert entry["_denied_keys"] == ["authorization"]
        assert _SECRET not in repr(entry)


class TestRuleApplication:
    """ロガーに設定した規則を適用し、ログ入力による規則の変更を認めない。"""

    def test_logger_without_policy_drops_every_key(self, configure_chain) -> None:
        """ポリシー未宣言の logger は業務キーを出さず、未登録件数だけ残す。"""
        capture = configure_chain()
        logger = structlog.get_logger("test")
        logger.info("fetch_done", source_id=1, url="https://example.com/a")
        entry = capture.entries[0]
        assert entry["_unregistered_count"] == 2
        assert "log_policy" not in entry
        assert not {"source_id", "url"} & entry.keys()

    def test_explicit_base_rules_keep_only_base_fields(self, configure_chain) -> None:
        """基底ルールを明示したloggerは基本項目だけを残して認証情報を除外する。"""
        capture = configure_chain()
        policy_logger("test", BASE_LOG_RULES).info(
            "completed", count=3, password=_SECRET
        )
        entry = capture.entries[0]
        assert entry["event"] == "completed"
        assert entry["_denied_keys"] == ["password"]
        assert entry["_unregistered_count"] == 1
        assert "count" not in entry
        assert "password" not in entry
        assert "log_policy" not in entry

    def test_declared_policy_name_is_emitted(self, configure_chain) -> None:
        """宣言したポリシーは `log_policy` として出力に残る。"""
        capture = configure_chain()
        logger = policy_logger("test", _TEST_RULES)
        logger.info("fetch_done", source_id=1)
        assert capture.entries[0]["log_policy"] == "pipeline_control"

    def test_processor_uses_completed_rules_without_rebuilding(
        self, monkeypatch
    ) -> None:
        """ロガーが持つ完成済みの規則を繰り返し出力しても再構築しない。"""
        parent = LogPolicyRules(
            LogPolicy.INFRASTRUCTURE, frozenset(), frozenset({"restricted_sample"})
        )
        rules = parent.extend(allow=frozenset({"count"}))

        def reject_reconstruction(self):
            pytest.fail("完成済みの規則を再構築した")

        monkeypatch.setattr(LogPolicyRules, "__post_init__", reject_reconstruction)
        processor = LogPolicyProcessor()
        event = {
            "event": "completed",
            "count": 3,
            "restricted_sample": "synthetic private value",
        }
        expected = {
            "event": "completed",
            "count": 3,
            "log_policy": "infrastructure",
            "_denied_keys": ["restricted_sample"],
        }
        assert (
            processor(PolicyLogger(rules, structlog.ReturnLogger()), "info", event)
            == expected
        )
        assert (
            processor(PolicyLogger(rules, structlog.ReturnLogger()), "info", event)
            == expected
        )

    def test_loggers_with_same_policy_use_their_bound_rules(
        self, configure_chain
    ) -> None:
        """同じ識別子のlogger同士でも、それぞれが受け取った規則を適用する。"""
        first = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({"first_count"}))
        last = first.extend(allow=frozenset({"last_count"}))
        capture = configure_chain()
        first_logger = policy_logger("first", first)
        last_logger = policy_logger("last", last)
        first_logger.info("completed", first_count=1, last_count=2)
        last_logger.info("completed", first_count=1, last_count=2)
        assert capture.entries[0]["first_count"] == 1
        assert "last_count" not in capture.entries[0]
        assert capture.entries[1]["last_count"] == 2
        assert "first_count" not in capture.entries[1]

    def test_policy_cannot_be_selected_from_call_site_string(
        self, configure_chain
    ) -> None:
        """呼び出し kwargs の文字列ではポリシーを選べない。"""
        capture = configure_chain()
        logger = structlog.get_logger("test")
        logger.info("fetch_done", _log_policy_rules="pipeline_control", source_id=1)
        entry = capture.entries[0]
        assert "log_policy" not in entry
        assert entry["_unregistered_count"] == 1

    def test_event_policy_identifier_does_not_select_rules(self) -> None:
        """ログ内の識別子をルールとして使わずロガーの基本ルールで保護する。"""
        processor = LogPolicyProcessor()
        output = processor(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {
                "_log_policy_rules": LogPolicy.INFRASTRUCTURE,
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

    def test_legacy_rule_field_never_reaches_renderer(self, configure_chain) -> None:
        """旧内部キーをallowと引数へ渡してもルールオブジェクトは出力しない。"""
        capture = configure_chain()
        rules = _TEST_RULES.extend(allow=frozenset({"_log_policy_rules"}))
        policy_logger("test", rules).info("completed", _log_policy_rules=BASE_LOG_RULES)
        assert "_log_policy_rules" not in capture.entries[0]
        assert "_unregistered_count" not in capture.entries[0]
        assert capture.entries[0]["log_policy"] == "pipeline_control"


class TestFieldSelection:
    """キー名の正規化と完全一致で項目を選別し、未登録項目を除外する。"""

    def test_camel_case_variant_of_denied_key_is_dropped(self, configure_chain) -> None:
        """`apiKey` は禁止キーとして落ち、値はログに出ない。"""
        capture = configure_chain()
        logger = policy_logger("test", _TEST_RULES)
        logger.info("fetch_failed", apiKey=_SECRET)
        entry = capture.entries[0]
        assert "apiKey" not in entry
        assert _SECRET not in repr(entry)

    def test_exact_deny_keeps_similar_allowed_key(self, configure_chain) -> None:
        """`token` の値は出ず、名前が似ていても許可した `completion_tokens` は残る。"""
        rules = LogPolicyRules(
            LogPolicy.PIPELINE_CONTROL, frozenset({"completion_tokens"})
        )
        capture = configure_chain()
        logger = policy_logger("test", rules)
        logger.info("usage", token=_SECRET, completion_tokens=128)
        entry = capture.entries[0]
        assert "token" not in entry
        assert entry["completion_tokens"] == 128
        assert _SECRET not in repr(entry)

    def test_unregistered_key_is_dropped_and_counted(self, configure_chain) -> None:
        """未登録キーは名前も値も出さず、件数だけを残す。"""
        capture = configure_chain()
        logger = policy_logger("test", _TEST_RULES)
        logger.info("fetch_done", source_id=1, elapsed_ms=12)
        entry = capture.entries[0]
        assert entry["_unregistered_count"] == 1
        assert "elapsed_ms" not in entry


class TestFieldProtection:
    """採用した値の秘密情報・ネスト内の禁止項目・不正な構造を出力に残さない。"""

    def test_credential_inside_allowed_text_is_sanitized(self, configure_chain) -> None:
        """許可した自由文に混入した credential だけを伏せ、原因文は残す。"""
        capture = configure_chain()
        logger = policy_logger("test", _INFRA_RULES)
        logger.warning(
            "db_connect_failed",
            error_message=(
                f"connection to postgresql://vector:{_SECRET}@db:5432/v refused"
            ),
        )
        message = capture.entries[0]["error_message"]
        assert _SECRET not in message
        assert message.startswith("connection to postgresql://***@db:5432/v refused")

    def test_event_string_is_sanitized(self, configure_chain) -> None:
        """event 文字列に混入した credential も置換される。"""
        capture = configure_chain()
        logger = policy_logger("test", _TEST_RULES)
        logger.info(f"failed with Authorization: Bearer {_SECRET}abcdef0123456789")
        assert _SECRET not in capture.entries[0]["event"]

    @pytest.mark.parametrize(
        "key", ["event", "level", "timestamp", "logger", "logger_name", "loggerName"]
    )
    def test_base_fields_use_normal_value_preparation(self, key: str) -> None:
        """基本項目も通常の構造検査・deny除外・サニタイズを通す。"""
        fields = {
            key: {"password": "synthetic", "message": "token=synthetic", "count": 1}
        }
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()), "info", fields
        )
        assert output == {
            key: {"message": "token=***", "count": 1},
            "_denied_nested_count": 1,
        }

    def test_denied_key_nested_in_allowed_dict_is_dropped_and_counted(
        self,
        configure_chain,
    ) -> None:
        """許可した dict の中の禁止キーは落ち、名前ではなく件数だけ残る。"""
        capture = configure_chain()
        logger = policy_logger("test", _TEST_RULES)
        logger.info(
            "article_converted",
            sample_length=len(_PRIVATE_SAMPLE),
            payload={
                "id": 7,
                "restricted_sample": _PRIVATE_SAMPLE,
                "meta": {"restricted_nested": "<p>x</p>"},
            },
        )
        entry = capture.entries[0]
        assert entry["payload"] == {"id": 7, "meta": {}}
        assert entry["_denied_nested_count"] == 2
        assert entry["sample_length"] == len(_PRIVATE_SAMPLE)
        assert _PRIVATE_SAMPLE not in repr(entry)

    def test_denied_key_nested_in_list_items_is_dropped(self, configure_chain) -> None:
        """list 要素の dict にも、同じ禁止キーの除外が適用される。"""
        capture = configure_chain()
        logger = policy_logger("test", _TEST_RULES)
        logger.info(
            "feed_parsed",
            items=[{"id": 1, "restricted_sample": _PRIVATE_SAMPLE}, {"id": 2}],
        )
        entry = capture.entries[0]
        assert entry["items"] == [{"id": 1}, {"id": 2}]
        assert entry["_denied_nested_count"] == 1

    def test_nonstring_mapping_is_replaced_without_losing_other_fields(self) -> None:
        """非文字列キーを含む辞書全体を置換し、外側の正常な項目は保持する。"""
        output = LogPolicyProcessor()(
            PolicyLogger(_TEST_RULES, structlog.ReturnLogger()),
            "info",
            {
                "event": "completed",
                "payload": {200: "synthetic", "count": 1},
                "source_id": 7,
            },
        )
        assert output == {
            "event": "completed",
            "payload": "[non-string-key]",
            "source_id": 7,
            "log_policy": _TEST_RULES.policy.value,
        }


class TestDiagnostics:
    """除外の診断を安全に出力し、ログごとに独立して保持する。"""

    def test_top_level_and_nested_diagnostics_are_combined(self) -> None:
        """トップレベルとネストで起きた除外を同じログへ集約する。"""
        output = LogPolicyProcessor()(
            PolicyLogger(_TEST_RULES, structlog.ReturnLogger()),
            "info",
            {
                "event": "completed",
                "password": _SECRET,
                "unknown": 1,
                "payload": {"token": _SECRET, "count": 2},
            },
        )
        assert output == {
            "event": "completed",
            "payload": {"count": 2},
            "log_policy": _TEST_RULES.policy.value,
            "_denied_keys": ["password"],
            "_unregistered_count": 1,
            "_denied_nested_count": 1,
        }

    def test_unregistered_key_name_is_not_echoed(self) -> None:
        """未登録の入力キー名そのものを診断フィールドへコピーしない。"""
        key_name = "synthetic private value"
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": "failed", key_name: "ignored"},
        )
        assert output["_unregistered_count"] == 1
        assert key_name not in output
        assert key_name not in repr(output)

    def test_denied_key_names_are_sanitized_before_output(self) -> None:
        """診断に記録した入力由来の禁止キー名もサニタイズして出力する。"""
        key = "token=synthetic"
        rules = LogPolicyRules(
            LogPolicy.PIPELINE_CONTROL, allow=frozenset(), deny=frozenset({key})
        )
        output = LogPolicyProcessor()(
            PolicyLogger(rules, structlog.ReturnLogger()),
            "info",
            {"event": "completed", key: _SECRET},
        )
        assert output["_denied_keys"] == ["token=***"]

    def test_diagnostics_do_not_leak_between_processor_calls(self) -> None:
        """同じprocessorで次のログを処理しても前回の診断は残らない。"""
        processor = LogPolicyProcessor()
        processor(
            PolicyLogger(_TEST_RULES, structlog.ReturnLogger()),
            "info",
            {
                "password": _SECRET,
                "unknown": 1,
                "payload": {"token": _SECRET},
                "x" * 4001: 1,
            },
        )
        assert processor(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": "completed"},
        ) == {"event": "completed"}

    def test_budget_overflow_does_not_affect_next_log(self) -> None:
        """共有予算超過の後も同じprocessorで正常なログを処理できる。"""
        processor = LogPolicyProcessor()
        processor(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {f"unknown_{i}": 1 for i in range(MAX_ITEMS_PER_LOG_EVENT + 1)},
        )
        assert processor(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": "completed"},
        ) == {"event": "completed"}

    def test_diagnostics_are_prepared_outside_value_preparer(self, monkeypatch) -> None:
        """値準備には採用された通常項目だけを渡し、診断は別の入口で準備する。"""
        from app.log_policy.value_preparation import LogValuePreparer

        prepared_inputs = []
        original = LogValuePreparer.prepare_field_value

        def capture_value(self, field_value):
            prepared_inputs.append(field_value)
            return original(self, field_value)

        monkeypatch.setattr(LogValuePreparer, "prepare_field_value", capture_value)
        prepared_event = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": "completed", "password": _SECRET},
        )
        assert prepared_inputs == ["completed"]
        assert prepared_event == {"event": "completed", "_denied_keys": ["password"]}


class TestExceptionFields:
    """例外項目を保護して通常項目と併記し、生成した例外項目を同名入力より優先する。"""

    def test_normal_and_exception_fields_are_output_with_secrets_masked(self) -> None:
        """通常項目と例外項目を併記するとき、両方の秘密値をマスクして出力する。"""
        output = LogPolicyProcessor()(
            PolicyLogger(_TEST_RULES, structlog.ReturnLogger()),
            "error",
            {
                "event": "failed",
                "payload": {"message": "password=normal-secret", "count": 3},
                "exc_info": ValueError("token=exception-secret"),
            },
        )
        assert output == {
            "event": "failed",
            "payload": {"message": "password=***", "count": 3},
            "error_class": "builtins.ValueError",
            "error_message": "token=***",
            "frames": [],
            "log_policy": _TEST_RULES.policy.value,
        }

    def test_exception_message_is_sanitized_in_output(self) -> None:
        """例外メッセージ内のAPIキーを伏せ、周囲の原因文を残して出力する。"""
        message = "request with sk-proj-abcdef0123456789ABCDEFxyz failed"
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "error",
            {"event": "failed", "exc_info": ValueError(message)},
        )
        assert output == {
            "event": "failed",
            "error_class": "builtins.ValueError",
            "error_message": "request with sk-*** failed",
            "frames": [],
        }

    def test_generated_exception_message_replaces_same_named_input(self) -> None:
        """同名の入力があっても、例外から抽出した項目を出力に採用する。"""
        output = LogPolicyProcessor()(
            PolicyLogger(_INFRA_RULES, structlog.ReturnLogger()),
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
            "error_message": "token=***",
            "frames": [],
            "log_policy": "infrastructure",
        }

    @pytest.mark.parametrize("exc_info", [None, False, "invalid"])
    def test_invalid_exception_info_preserves_input_error_message(
        self, exc_info
    ) -> None:
        """例外項目を生成できない場合は、入力の同名項目を通常どおり準備する。"""
        prepared_event = LogPolicyProcessor()(
            PolicyLogger(_INFRA_RULES, structlog.ReturnLogger()),
            "error",
            {"error_message": "token=synthetic", "exc_info": exc_info},
        )
        assert prepared_event == {
            "error_message": "token=***",
            "log_policy": "infrastructure",
        }


class TestProcessingOrder:
    """採用した値を項目ごとに順に準備し、不採用の値は準備処理へ渡さない。"""

    def test_each_field_value_is_prepared_before_reading_next_field(
        self, monkeypatch
    ) -> None:
        """一つの項目を選別して値を準備し終えるまで次の項目を取り出さない。"""
        from app.log_policy.value_preparation import LogValuePreparer

        steps = []
        original = LogValuePreparer.prepare_field_value

        class EventFields(dict):
            def items(self):
                steps.append("read_payload")
                yield "payload", {"message": "token=synthetic"}
                steps.append("read_event")
                yield "event", "completed"

        def prepare_value(self, field_value):
            prepared_value = original(self, field_value)
            steps.append(prepared_value)
            return prepared_value

        monkeypatch.setattr(LogValuePreparer, "prepare_field_value", prepare_value)
        LogPolicyProcessor()(
            PolicyLogger(_TEST_RULES, structlog.ReturnLogger()), "info", EventFields()
        )
        assert steps == [
            "read_payload",
            {"message": "token=***"},
            "read_event",
            "completed",
        ]

    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            ({"password": object()}, {"_denied_keys": ["password"]}),
            ({"unknown": object()}, {"_unregistered_count": 1}),
            ({"x" * (TEXT_LIMIT + 1): object()}, {"_policy_limited": True}),
        ],
        ids=["denied", "unregistered", "long_name"],
    )
    def test_excluded_field_value_is_not_prepared(
        self, monkeypatch, fields: dict, expected: dict
    ) -> None:
        """禁止・未登録・長すぎる名前の項目の値は構造検査やサニタイズへ渡さず除外する。"""
        from app.log_policy.value_preparation import LogValuePreparer

        def unexpected_preparation(self, field_value):
            raise AssertionError("excluded value must not be prepared")

        monkeypatch.setattr(
            LogValuePreparer, "prepare_field_value", unexpected_preparation
        )
        prepared_event = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()), "info", fields
        )
        assert prepared_event == expected

    def test_top_level_budget_overflow_stops_input_iteration(self) -> None:
        """上限超過を確認した一件より後の入力は取り出さない。"""

        class BoundedInput(dict):
            def items(self):
                for i in range(MAX_ITEMS_PER_LOG_EVENT + 1):
                    yield f"unknown_{i}", 1
                raise AssertionError("must not read after budget overflow")

        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            BoundedInput(),
        )
        assert output == {
            "event": "log_policy_budget_exceeded",
            "_policy_limited": True,
            "_policy_limit_reason": "value_count",
        }

    def test_nested_budget_overflow_stops_before_next_top_level_field(self) -> None:
        """値の走査で予算を超えたら、次のトップレベル項目も取り出さない。"""

        class EventFields(dict):
            def items(self):
                yield "payload", [1] * MAX_ITEMS_PER_LOG_EVENT
                raise AssertionError("must not read next field after nested overflow")

        prepared_event = LogPolicyProcessor()(
            PolicyLogger(_TEST_RULES, structlog.ReturnLogger()), "info", EventFields()
        )
        assert prepared_event == {
            "event": "log_policy_budget_exceeded",
            "_policy_limited": True,
            "_policy_limit_reason": "value_count",
        }


class TestProcessingFailures:
    """処理に失敗した場合は原文を出力せず固定エラーを返す。"""

    def test_processor_without_policy_logger_returns_fixed_error(self) -> None:
        """ルールを持つロガーの接続が欠けても原文を返さず固定エラーにする。"""
        assert LogPolicyProcessor()(
            structlog.ReturnLogger(), "info", {"event": _SECRET}
        ) == {"event": "log_policy_failed", "_policy_error": "processing_failed"}

    def test_diagnostic_sanitization_failure_does_not_restore_raw_key_names(
        self,
        monkeypatch,
    ) -> None:
        """診断のサニタイズが失敗しても原文に戻らず固定エラーだけを返す。"""

        def fail(text):
            raise ValueError("synthetic-private-key")

        monkeypatch.setattr("app.log_policy.diagnostics.sanitize_text", fail)
        prepared_event = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "info",
            {"event": "completed", "password": _SECRET},
        )
        assert prepared_event == {
            "event": "log_policy_failed",
            "_policy_error": "processing_failed",
        }

    def test_value_sanitization_failure_returns_only_fixed_metadata(
        self, monkeypatch
    ) -> None:
        """値のサニタイズが失敗しても業務側へ例外や原文を返さない。"""

        def fail(*_, **__):
            raise ValueError(_SECRET)

        monkeypatch.setattr("app.log_policy.value_preparation.sanitize_text", fail)
        output = LogPolicyProcessor()(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
            "error",
            {"event": _SECRET},
        )
        assert output == {
            "event": "log_policy_failed",
            "_policy_error": "processing_failed",
        }

    def test_processing_failure_emits_one_fixed_event_without_reentry(
        self, monkeypatch, configure_chain
    ) -> None:
        """保護処理の失敗を保護処理自身で記録せず、固定イベント1件だけを出す。"""

        def fail(*_, **__):
            raise ValueError(_SECRET)

        monkeypatch.setattr("app.log_policy.value_preparation.sanitize_text", fail)
        processed_methods = []
        original = LogPolicyProcessor.__call__

        def counted(self, logger, method_name, event_dict):
            processed_methods.append(method_name)
            return original(self, logger, method_name, event_dict)

        monkeypatch.setattr(LogPolicyProcessor, "__call__", counted)
        capture = configure_chain()
        policy_logger("test", BASE_LOG_RULES).error(_SECRET)
        assert [dict(entry) for entry in capture.entries] == [
            {
                "event": "log_policy_failed",
                "_policy_error": "processing_failed",
                "log_level": "error",
            }
        ]
        assert processed_methods == ["error"]
