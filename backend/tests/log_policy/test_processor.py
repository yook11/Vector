"""実チェーンを通したログ出力の契約。合成値のみを使う。"""

from __future__ import annotations

import pytest
import structlog

from app.log_policy import LogPolicy, LogPolicyRules, policy_logger
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


def test_denied_key_passed_as_argument_is_dropped(configure_chain) -> None:
    """ログ引数の禁止キーは値が出ず、名前だけ `_denied_keys` に残る。"""
    capture = configure_chain([_TEST_RULES])
    logger = policy_logger("test", LogPolicy.PIPELINE_CONTROL)
    logger.info("fetch_failed", source_id=1, password=_SECRET)
    entry = capture.entries[0]
    assert entry["_denied_keys"] == ["password"]
    assert _SECRET not in repr(entry)


def test_denied_key_passed_via_bind_is_dropped(configure_chain) -> None:
    """bind した禁止キーも、ログ引数と同じく値が出ずに名前だけ残る。"""
    capture = configure_chain([_TEST_RULES])
    logger = policy_logger("test", LogPolicy.PIPELINE_CONTROL)
    logger.bind(api_key=_SECRET).info("fetch_failed", source_id=1)
    entry = capture.entries[0]
    assert entry["_denied_keys"] == ["api_key"]
    assert _SECRET not in repr(entry)


def test_denied_key_passed_via_contextvars_is_dropped(configure_chain) -> None:
    """contextvars の禁止キーも、ログ引数と同じく値が出ずに名前だけ残る。"""
    capture = configure_chain([_TEST_RULES])
    structlog.contextvars.bind_contextvars(authorization=f"Bearer {_SECRET}")
    logger = policy_logger("test", LogPolicy.PIPELINE_CONTROL)
    logger.info("fetch_failed", source_id=1)
    entry = capture.entries[0]
    assert entry["_denied_keys"] == ["authorization"]
    assert _SECRET not in repr(entry)


def test_camel_case_variant_of_denied_key_is_dropped(configure_chain) -> None:
    """`apiKey` は禁止キーとして落ち、値はログに出ない。"""
    capture = configure_chain([_TEST_RULES])
    logger = policy_logger("test", LogPolicy.PIPELINE_CONTROL)
    logger.info("fetch_failed", apiKey=_SECRET)
    entry = capture.entries[0]
    assert "apiKey" not in entry
    assert _SECRET not in repr(entry)


def test_exact_deny_keeps_similar_allowed_key(configure_chain) -> None:
    """`token` の値は出ず、名前が似ていても許可した `completion_tokens` は残る。"""
    rules = LogPolicyRules(LogPolicy.PIPELINE_CONTROL, frozenset({"completion_tokens"}))
    capture = configure_chain([rules])
    logger = policy_logger("test", LogPolicy.PIPELINE_CONTROL)
    logger.info("usage", token=_SECRET, completion_tokens=128)
    entry = capture.entries[0]
    assert "token" not in entry
    assert entry["completion_tokens"] == 128
    assert _SECRET not in repr(entry)


def test_identifier_values_are_kept_unchanged_when_allowed(configure_chain) -> None:
    """許可した ARN / endpoint は識別情報なので、基底は置換しない。"""
    capture = configure_chain([_INFRA_RULES])
    arn = "arn:aws:ssm:ap-northeast-1:123456789012:parameter/vector/db"
    endpoint = "vector-db.cluster-abc.ap-northeast-1.rds.amazonaws.com"
    logger = policy_logger("test", LogPolicy.INFRASTRUCTURE)
    logger.info("ssm_parameter_read", resource=arn, endpoint=endpoint)
    entry = capture.entries[0]
    assert (entry["resource"], entry["endpoint"]) == (arn, endpoint)


def test_credential_inside_allowed_text_is_sanitized(configure_chain) -> None:
    """許可した自由文に混入した credential だけを伏せ、原因文は残す。"""
    capture = configure_chain([_INFRA_RULES])
    logger = policy_logger("test", LogPolicy.INFRASTRUCTURE)
    logger.warning(
        "db_connect_failed",
        error_message=f"connection to postgresql://vector:{_SECRET}@db:5432/v refused",
    )
    message = capture.entries[0]["error_message"]
    assert _SECRET not in message
    assert message.startswith("connection to postgresql://***@db:5432/v refused")


def test_denied_key_nested_in_allowed_dict_is_dropped_and_counted(
    configure_chain,
) -> None:
    """許可した dict の中の禁止キーは落ち、名前ではなく件数だけ残る。"""
    capture = configure_chain([_TEST_RULES])
    logger = policy_logger("test", LogPolicy.PIPELINE_CONTROL)
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


def test_denied_key_nested_in_list_items_is_dropped(configure_chain) -> None:
    """list 要素の dict にも、同じ禁止キーの除外が適用される。"""
    capture = configure_chain([_TEST_RULES])
    logger = policy_logger("test", LogPolicy.PIPELINE_CONTROL)
    logger.info(
        "feed_parsed",
        items=[{"id": 1, "restricted_sample": _PRIVATE_SAMPLE}, {"id": 2}],
    )
    entry = capture.entries[0]
    assert entry["items"] == [{"id": 1}, {"id": 2}]
    assert entry["_denied_nested_count"] == 1


def test_unregistered_key_is_dropped_and_counted(configure_chain) -> None:
    """未登録キーは名前も値も出さず、件数だけを残す。"""
    capture = configure_chain([_TEST_RULES])
    logger = policy_logger("test", LogPolicy.PIPELINE_CONTROL)
    logger.info("fetch_done", source_id=1, elapsed_ms=12)
    entry = capture.entries[0]
    assert entry["_unregistered_count"] == 1
    assert "elapsed_ms" not in entry


def test_unregistered_key_name_is_not_echoed() -> None:
    """未登録の入力キー名そのものを診断フィールドへコピーしない。"""
    key_name = "synthetic private value"
    output = LogPolicyProcessor()(
        None, "info", {"event": "failed", key_name: "ignored"}
    )
    assert output["_unregistered_count"] == 1
    assert key_name not in output
    assert key_name not in repr(output)


def test_logger_without_policy_drops_every_key(configure_chain) -> None:
    """ポリシー未宣言の logger は業務キーを出さず、未登録件数だけ残す。"""
    capture = configure_chain([_TEST_RULES])
    logger = structlog.get_logger("test")
    logger.info("fetch_done", source_id=1, url="https://example.com/a")
    entry = capture.entries[0]
    assert entry["_unregistered_count"] == 2
    assert "log_policy" not in entry
    assert not {"source_id", "url"} & entry.keys()


def test_policy_cannot_be_selected_from_call_site_string(configure_chain) -> None:
    """呼び出し kwargs の文字列ではポリシーを選べない。"""
    capture = configure_chain([_TEST_RULES])
    logger = structlog.get_logger("test")
    logger.info("fetch_done", _log_policy="pipeline_control", source_id=1)
    entry = capture.entries[0]
    assert "log_policy" not in entry
    assert entry["_unregistered_count"] == 1


def test_declared_policy_name_is_emitted(configure_chain) -> None:
    """宣言したポリシーは `log_policy` として出力に残る。"""
    capture = configure_chain([_TEST_RULES])
    logger = policy_logger("test", LogPolicy.PIPELINE_CONTROL)
    logger.info("fetch_done", source_id=1)
    assert capture.entries[0]["log_policy"] == "pipeline_control"


def test_event_string_is_sanitized(configure_chain) -> None:
    """event 文字列に混入した credential も置換される。"""
    capture = configure_chain([_TEST_RULES])
    logger = policy_logger("test", LogPolicy.PIPELINE_CONTROL)
    logger.info(f"failed with Authorization: Bearer {_SECRET}abcdef0123456789")
    assert _SECRET not in capture.entries[0]["event"]
