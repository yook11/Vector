"""秘密情報取得のポリシーを通したJSON出力を確認する。"""

import json
from functools import partial

import pytest
import structlog

from app.log_policy import build_processors, create_policy_logger, policy_logger

pytestmark = pytest.mark.unit


@pytest.fixture
def logger_with_secret_access_policy(configure_chain):
    from app.log_policy.policies.secret_access import SECRET_ACCESS_LOG_RULES

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
        "test.logger_with_secret_access_policy", SECRET_ACCESS_LOG_RULES
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
        ("operation", "cleanup"),
        ("resource", "ssm"),
        ("error_class", "builtins.RuntimeError"),
    ],
)
def test_secret_access_field_is_output(
    logger_with_secret_access_policy, capsys, allowed_field, value
):
    """cleanupの処理箇所・資源名・例外型・相関情報は、渡した値でログに残る。"""
    logger_with_secret_access_policy.warning("policy_test", **{allowed_field: value})

    log_entry = json.loads(capsys.readouterr().out)

    assert log_entry[allowed_field] == value


@pytest.mark.parametrize(
    "excluded_field",
    ["model", "tags", "path", "value", "response"],
)
def test_unnecessary_or_private_field_is_excluded(
    logger_with_secret_access_policy, capsys, excluded_field
):
    """モデル・通知対象・パラメーターパス・秘密値・応答全体は取得ログへ出さない。"""
    private_value = "PRIVATE_SECRET_VALUE"
    logger_with_secret_access_policy.warning(
        "policy_test", **{excluded_field: private_value}
    )

    output = capsys.readouterr().out
    log_entry = json.loads(output)

    assert excluded_field not in log_entry
    assert private_value not in output
