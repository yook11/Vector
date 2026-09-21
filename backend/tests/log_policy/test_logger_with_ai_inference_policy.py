"""AI推論ポリシーのallow・deny・maskが実際のJSON標準出力に適用される。"""

import json
from functools import partial

import pytest
import structlog

from app.log_policy import build_processors, create_policy_logger, policy_logger
from app.log_policy.base import BASE_ALLOW
from app.log_policy.exceptions.application import convert_application_exception
from app.log_policy.policies.ai_inference import AI_INFERENCE_LOG_RULES

pytestmark = pytest.mark.unit

# 保護対象が設定から消えても検出できるよう、仕様の本文10項目を明示する。
ARTICLE_FIELDS_TO_PROTECT = (
    "body",
    "content",
    "text",
    "html",
    "description",
    "summary",
    "translation",
    "key_points",
    "snippet",
    "answer",
)


@pytest.fixture
def logger_with_ai_inference_policy(configure_chain):
    """製品のAI推論ルール・factory・processor・JSON整形を標準出力へ接続する。"""
    configure_chain()
    structlog.contextvars.clear_contextvars()
    structlog.configure(
        logger_factory=partial(
            create_policy_logger,
            output_logger_factory=structlog.WriteLoggerFactory(),
        ),
        processors=build_processors(
            structlog.processors.JSONRenderer(),
            exception_converter=convert_application_exception,
        ),
    )
    return policy_logger("test.logger_with_ai_inference_policy", AI_INFERENCE_LOG_RULES)


@pytest.mark.parametrize(
    "allowed_field",
    sorted(AI_INFERENCE_LOG_RULES.allow - BASE_ALLOW),
)
def test_allowed_field_is_output(
    logger_with_ai_inference_policy, capsys, allowed_field
) -> None:
    """ポリシーのallowで許可された項目は、渡した値でログに出力される。"""
    value = "synthetic_log_value"

    logger_with_ai_inference_policy.info(
        "policy_test",
        **{allowed_field: value},
    )

    log_entry = json.loads(capsys.readouterr().out)

    assert log_entry[allowed_field] == value


@pytest.mark.parametrize("denied_field", ARTICLE_FIELDS_TO_PROTECT)
def test_denied_field_is_excluded(
    logger_with_ai_inference_policy, capsys, denied_field
) -> None:
    """ポリシーのdenyで禁止された項目は除外し、許可された項目はログに出力する。"""
    private_value = "PRIVATE_ARTICLE_VALUE"

    logger_with_ai_inference_policy.info(
        "policy_test",
        message_id="message-001",
        **{denied_field: private_value},
    )

    output = capsys.readouterr().out
    log_entry = json.loads(output)

    assert denied_field not in log_entry
    assert private_value not in output
    assert denied_field in log_entry["_denied_keys"]
    assert log_entry["message_id"] == "message-001"


@pytest.mark.parametrize("masked_field", ARTICLE_FIELDS_TO_PROTECT)
def test_masked_assignment_hides_value(
    logger_with_ai_inference_policy, capsys, masked_field
) -> None:
    """ポリシーのmaskで指定された項目の値は文字列内で伏せ、前後の内容はログに出力する。"""
    private_value = "PRIVATE_ARTICLE_VALUE"

    logger_with_ai_inference_policy.info(
        f"before {masked_field}='{private_value}' after",
    )

    output = capsys.readouterr().out
    log_entry = json.loads(output)

    assert log_entry["event"] == f"before {masked_field}=*** after"
    assert private_value not in output
