"""JSON と console の renderer で、秘匿後の値を整形しても原文が戻らない。"""

from __future__ import annotations

import json

import pytest
import structlog

from app.log_policy import LogPolicy, LogPolicyRules, policy_logger

pytestmark = pytest.mark.unit

_SECRET = "hunter2-synthetic-secret"
_ARTICLE_BODY = "記事本文の合成テキスト"

_RULES = LogPolicyRules(
    policy=LogPolicy.PIPELINE_CONTROL,
    allow=frozenset({"source_id", "reason"}),
    deny=frozenset({"content"}),
)


def _emit_redacted_failure(configure_chain) -> dict:
    """通常の logger 呼び出しをチェーン末尾の LogCapture で受け取る。"""
    capture = configure_chain([_RULES])
    logger = policy_logger("test", LogPolicy.PIPELINE_CONTROL)
    try:
        raise RuntimeError(f"connect failed password={_SECRET}")
    except RuntimeError:
        logger.error(
            "stage_failed",
            source_id=3,
            reason=f"token expired: Authorization: Bearer {_SECRET}abcdef0123456789",
            content=_ARTICLE_BODY,
            exc_info=True,
        )
    return dict(capture.entries[0])


def test_json_and_console_keep_the_same_redaction(configure_chain) -> None:
    """JSON と console のどちらでも、禁止値・秘密値・traceback は復活しない。"""
    entry = _emit_redacted_failure(configure_chain)
    assert entry["_denied_keys"] == ["content"]
    assert "content" not in entry
    assert entry["source_id"] == 3
    assert entry["error_class"] == "builtins.RuntimeError"
    assert "exc_info" not in entry
    assert _SECRET not in entry["reason"]

    json_output = structlog.processors.JSONRenderer()(None, "error", dict(entry))
    console_output = structlog.dev.ConsoleRenderer(colors=False)(
        None, "error", dict(entry)
    )
    record = json.loads(json_output)
    assert record["_denied_keys"] == ["content"]
    assert record["error_class"] == "builtins.RuntimeError"
    assert "_denied_keys" in console_output
    assert "builtins.RuntimeError" in console_output
    for output in (json_output, console_output):
        for leaked in (_SECRET, _ARTICLE_BODY, "Traceback"):
            assert leaked not in output
