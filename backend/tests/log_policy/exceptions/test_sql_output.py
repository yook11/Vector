"""SQL診断の最終出力と入力経路からの注入防止を検証する。"""

import json

import pytest
import structlog
from asyncpg import PostgresError
from sqlalchemy.exc import IntegrityError

from app.log_policy import BASE_LOG_RULES, LogPolicyRules, PolicyLogger, policy_logger
from app.log_policy.processor import LogPolicyProcessor

pytestmark = pytest.mark.unit


def test_sql_details_are_redacted_in_json_renderer(configure_chain) -> None:
    """診断属性もJSON化前に情報漏洩防止を通す。"""
    configure_chain()
    structlog.configure(
        processors=[LogPolicyProcessor(), structlog.processors.JSONRenderer()],
        logger_factory=lambda *_: PolicyLogger(
            BASE_LOG_RULES, structlog.ReturnLogger()
        ),
    )
    exc = IntegrityError(
        "synthetic-private-sql",
        ("synthetic-private-row",),
        PostgresError.new(
            {
                "C": "23505",
                "M": "duplicate",
                # 合成値を分割し、秘密検出ツールの規則に一致させない。
                "n": "AIza" + "SyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q",
                "D": "synthetic-private-row",
            }
        ),
    )
    output = json.loads(policy_logger("test").error("failed", exc_info=exc))
    assert output["error_details"] == {
        "kind": "postgresql",
        "sqlstate": "23505",
        "constraint_name": "[redacted:gemini_api_key]",
    }
    assert "sqlstate" not in output
    assert "synthetic-private" not in json.dumps(output)


@pytest.mark.parametrize("source", ["argument", "bind", "contextvars"])
def test_exception_details_cannot_be_injected(source, configure_chain) -> None:
    """allow宣言があっても入力経路の診断辞書を受け入れない。"""
    capture = configure_chain()
    rules = LogPolicyRules(None, frozenset({"error_details", "causes", "errorDetails"}))
    logger = policy_logger("test", rules)
    payload = {
        "error_details": {"unknown": "synthetic-private"},
        "causes": [{"error_message": "synthetic-private"}],
        "errorDetails": {"unknown": "synthetic-private"},
    }
    if source == "argument":
        logger.error("failed", **payload)
    elif source == "bind":
        logger.bind(**payload).error("failed")
    else:
        structlog.contextvars.bind_contextvars(**payload)
        logger.error("failed")
    assert "synthetic-private" not in json.dumps(capture.entries[0])
    assert "error_details" not in capture.entries[0]
    assert "causes" not in capture.entries[0]


def test_generated_details_replace_injected_details(configure_chain) -> None:
    """有効な例外がある場合も入力辞書を混ぜず、抽出した診断だけを出す。"""
    capture = configure_chain()
    exc = IntegrityError(
        "INSERT ...", (), PostgresError.new({"C": "23505", "M": "duplicate"})
    )
    policy_logger("test").error(
        "failed", exc_info=exc, error_details={"unknown": "synthetic-private"}
    )
    assert capture.entries[0]["error_details"] == {
        "kind": "postgresql",
        "sqlstate": "23505",
    }
    assert "synthetic-private" not in json.dumps(capture.entries[0])


def test_cause_details_are_redacted() -> None:
    """原因ノードの診断属性にも情報漏洩防止を適用する。"""
    cause = IntegrityError(
        "INSERT ...",
        (),
        PostgresError.new(
            {"C": "23505", "M": "duplicate", "n": "password='synthetic-private'"}
        ),
    )
    outer = RuntimeError("wrapped")
    outer.__cause__ = cause
    output = LogPolicyProcessor()(
        PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()),
        "error",
        {"event": "failed", "exc_info": outer},
    )
    assert output["causes"][0]["error_details"]["constraint_name"] == (
        "password=[redacted:credential]"
    )
