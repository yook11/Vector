"""記事分析ポリシーのallow・denyが実際のJSON標準出力に適用される。"""

import json

import pytest
import structlog

from app.log_policy.base import BASE_ALLOW
from app.log_policy.policies.article_analysis import ARTICLE_ANALYSIS_LOG_RULES
from app.log_policy.runtime import create_policy_json_logger

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
def logger_with_article_analysis_policy():
    """製品の記事分析ルールを、本番と同じ組み立てで標準出力のJSONへ接続する。"""
    structlog.contextvars.clear_contextvars()
    return create_policy_json_logger(
        "test.logger_with_article_analysis_policy", ARTICLE_ANALYSIS_LOG_RULES
    )


@pytest.mark.parametrize(
    "allowed_field",
    sorted(ARTICLE_ANALYSIS_LOG_RULES.allow - BASE_ALLOW),
)
def test_allowed_field_is_output(
    logger_with_article_analysis_policy, capsys, allowed_field
) -> None:
    """ポリシーのallowで許可された項目は、渡した値でログに出力される。"""
    value = "synthetic_log_value"

    logger_with_article_analysis_policy.info(
        "policy_test",
        **{allowed_field: value},
    )

    log_entry = json.loads(capsys.readouterr().out)

    assert log_entry[allowed_field] == value


@pytest.mark.parametrize("denied_field", ARTICLE_FIELDS_TO_PROTECT)
def test_denied_field_is_excluded(
    logger_with_article_analysis_policy, capsys, denied_field
) -> None:
    """ポリシーのdenyで禁止された項目は除外し、許可された項目はログに出力する。"""
    private_value = "PRIVATE_ARTICLE_VALUE"

    logger_with_article_analysis_policy.info(
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
