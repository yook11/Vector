"""キャッシュ更新通知のポリシーを通したJSON出力を確認する。"""

import json
from functools import partial

import pytest
import structlog

from app.log_policy import build_processors, create_policy_logger, policy_logger

pytestmark = pytest.mark.unit


@pytest.fixture
def logger_with_cache_revalidation_policy(configure_chain):
    from app.log_policy.policies.cache_revalidation import (
        CACHE_REVALIDATION_LOG_RULES,
    )

    configure_chain()
    structlog.contextvars.clear_contextvars()
    structlog.configure(
        logger_factory=partial(
            create_policy_logger,
            output_logger_factory=structlog.WriteLoggerFactory(),
        ),
        processors=build_processors(structlog.processors.JSONRenderer()),
    )
    return policy_logger(
        "test.logger_with_cache_revalidation_policy", CACHE_REVALIDATION_LOG_RULES
    )


@pytest.mark.parametrize(
    ("allowed_field", "value"),
    [
        ("service", "article_analysis"),
        ("environment", "test"),
        ("stage", "assessment"),
        ("request_id", "request-001"),
        ("message_id", "message-001"),
        ("event_id", "event-001"),
        ("tags", ["articles:list", "articles:categories"]),
        ("operation", "get_secret"),
        ("operation", "notify"),
        ("error_class", "httpx.ConnectError"),
    ],
)
def test_notification_field_is_output(
    logger_with_cache_revalidation_policy, capsys, allowed_field, value
):
    """通知対象・処理箇所・例外型・相関情報は、渡した値でログに残る。"""
    logger_with_cache_revalidation_policy.info("policy_test", **{allowed_field: value})

    log_entry = json.loads(capsys.readouterr().out)

    assert log_entry[allowed_field] == value


@pytest.mark.parametrize(
    "excluded_field",
    [
        "model",
        "curation_id",
        "frontend_base_url",
        "revalidate_bearer_secret",
        "response",
    ],
)
def test_unnecessary_or_private_field_is_excluded(
    logger_with_cache_revalidation_policy, capsys, excluded_field
):
    """分析固有の項目・内部宛先・認証キー・応答本文は通知ログへ出さない。"""
    private_value = "PRIVATE_NOTIFICATION_VALUE"
    logger_with_cache_revalidation_policy.warning(
        "policy_test", **{excluded_field: private_value}
    )

    output = capsys.readouterr().out
    log_entry = json.loads(output)

    assert excluded_field not in log_entry
    assert private_value not in output
