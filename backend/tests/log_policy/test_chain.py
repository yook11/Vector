"""JSON と console の renderer で、秘匿後の値を整形しても原文が戻らない。"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
import structlog
from structlog.testing import LogCapture

from app.log_policy import (
    BASE_LOG_RULES,
    LogPolicy,
    LogPolicyRules,
)
from app.log_policy.budget import MAX_ITEMS_PER_LOG_EVENT, TEXT_LIMIT
from app.log_policy.runtime import create_policy_json_logger, render_policy_json

pytestmark = pytest.mark.unit

_SECRET = "hunter2-synthetic-secret"
_ARTICLE_BODY = "記事本文の合成テキスト"

_RULES = LogPolicyRules(
    policy=LogPolicy.PIPELINE_CONTROL,
    allow=frozenset({"source_id", "reason", "detail", "payload"}),
    deny=frozenset({"content"}),
)
# 合成値を分割し、秘密検出ツールの規則に一致させない。
_GEMINI_KEY = "AIza" + "SyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q"
# 認証キーより後ろは末尾まで置き換わるため、キー付きの値を最後に置く。
_DETAIL_WITH_EVERY_CREDENTIAL_FORM = (
    f"key={_GEMINI_KEY} "
    "for AKIAIOSFODNN7EXAMPLE "
    "postgresql+asyncpg://user:secret@db:5432/vector "
    "got eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.abc failed "
    "password='synthetic private value' host=db"
)
_DETAIL_FRAGMENTS = (
    "synthetic private value",
    _GEMINI_KEY,
    "AKIAIOSFODNN7EXAMPLE",
    "user:secret@",
    "eyJhbGciOiJIUzI1NiJ9",
)


def _emit_redacted_failure(chain_logger) -> dict:
    """通常の logger 呼び出しをチェーン末尾の LogCapture で受け取る。"""
    capture = LogCapture()
    logger = chain_logger(_RULES, capture)
    try:
        raise RuntimeError(f"connect failed password={_SECRET}")
    except RuntimeError:
        logger.error(
            "stage_failed",
            source_id=3,
            reason=f"token expired: Authorization: Bearer {_SECRET}abcdef0123456789",
            detail=_DETAIL_WITH_EVERY_CREDENTIAL_FORM,
            content=_ARTICLE_BODY,
            payload={"wrapper": [{"content": [_ARTICLE_BODY], "status": "kept"}]},
            exc_info=True,
        )
    return dict(capture.entries[0])


def test_json_and_console_keep_the_same_redaction(chain_logger) -> None:
    """JSON と console のどちらでも、禁止値・各形式の認証情報・traceback は戻らない。"""
    entry = _emit_redacted_failure(chain_logger)
    assert entry["_denied_keys"] == ["content"]
    assert "content" not in entry
    assert entry["payload"] == {"wrapper": [{"status": "kept"}]}
    assert entry["_denied_nested_count"] == 1
    assert entry["source_id"] == 3
    assert entry["error_class"] == "builtins.RuntimeError"
    assert "exc_info" not in entry
    assert _SECRET not in entry["reason"]
    assert entry["detail"] == (
        "key=[redacted:gemini_api_key] for [redacted:aws_access_key_id] "
        "postgresql+asyncpg://[redacted:url_userinfo]@db:5432/vector "
        "got [redacted:jwt] failed password=[redacted:credential]"
    )

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
        for leaked in (_SECRET, _ARTICLE_BODY, "Traceback", *_DETAIL_FRAGMENTS):
            assert leaked not in output


def test_chain_generates_string_level_before_console_rendering() -> None:
    """入力のlevelが非文字列でも前段の生成処理が文字列へ置き換える。"""
    from app.log_policy import build_processors

    processors = build_processors(
        BASE_LOG_RULES, structlog.dev.ConsoleRenderer(colors=False)
    )
    entry = {"event": "completed", "level": {"invalid": 1}}
    for processor in processors[:-1]:
        entry = processor(None, "info", entry)

    assert entry["level"] == "info"
    rendered = processors[-1](None, "info", dict(entry))
    assert "completed" in rendered
    assert "invalid" not in rendered


@pytest.mark.parametrize(
    "renderer",
    [structlog.processors.JSONRenderer(), structlog.dev.ConsoleRenderer(colors=False)],
)
def test_structured_event_is_prepared_before_rendering(renderer) -> None:
    """辞書のeventも共通の秘匿処理を通してJSONとconsoleへ出せる。"""
    from app.log_policy import build_processors

    entry = {"event": {"password": _SECRET, "message": "token=synthetic", "count": 3}}
    processors = build_processors(BASE_LOG_RULES, renderer)
    for processor in processors[:-1]:
        entry = processor(None, "info", entry)

    assert entry["event"] == {
        "message": "token=[redacted:credential]",
        "count": 3,
    }
    rendered = processors[-1](None, "info", dict(entry))
    assert _SECRET not in rendered
    assert "synthetic" not in rendered


def test_production_logger_outputs_redacted_json(capsys) -> None:
    """本番の組み立てで作ったロガーも、秘匿済みのログをJSONで標準出力へ出す。"""
    logger = create_policy_json_logger("test", _RULES)
    logger.info("completed", reason=f"token={_SECRET}")
    rendered = capsys.readouterr().out
    assert json.loads(rendered)["reason"] == "token=[redacted:credential]"
    assert _SECRET not in rendered


@pytest.mark.parametrize(
    "value", [_SECRET.encode(), float("nan")], ids=["bytes", "nan"]
)
def test_production_renderer_drops_value_it_cannot_serialize(value: object) -> None:
    """JSONにできない値を受けても、本番のrendererは例外を投げず、その値を含まないJSONの1行を返す。"""
    rendered = render_policy_json(
        None, "info", {"event": "completed", "payload": value}
    )
    assert "payload" not in json.loads(rendered)


def test_zero_value_is_preserved_in_json_output(chain_logger) -> None:
    """許可された項目の0は未取得として除外せず、JSONへ数値のまま出力する。"""
    logger = chain_logger(_RULES, structlog.processors.JSONRenderer())

    log_entry = json.loads(logger.info("completed", source_id=0))

    assert type(log_entry["source_id"]) is int
    assert log_entry["source_id"] == 0


def test_console_renderer_outputs_redacted_log(chain_logger) -> None:
    """consoleの実チェーンでも秘匿済みのログを出す。"""
    logger = chain_logger(_RULES, structlog.dev.ConsoleRenderer(colors=False))
    rendered = logger.info("completed", reason=f"token={_SECRET}")
    assert "completed" in rendered
    assert "token=[redacted:credential]" in rendered
    assert _SECRET not in rendered


@pytest.mark.parametrize(
    "renderer",
    [
        structlog.processors.JSONRenderer(),
        structlog.dev.ConsoleRenderer(colors=False),
    ],
)
def test_field_limit_marker_is_rendered_without_raw_content(
    chain_logger, renderer
) -> None:
    """JSONとconsoleの実チェーンでも超過項目の原文を出さず正常なeventを残す。"""
    rules = LogPolicyRules(LogPolicy.PROCESS_RUNTIME, frozenset({"payload"}))
    capture = LogCapture()
    chain_logger(rules, capture).info(
        "failed", payload=["synthetic private" * TEXT_LIMIT]
    )
    assert capture.entries[0]["payload"] == ["[limit]"]
    rendered = renderer(None, "info", dict(capture.entries[0]))
    assert "synthetic private" not in rendered
    assert "[limit]" in rendered
    assert "failed" in rendered


@pytest.mark.parametrize(
    "renderer",
    [
        structlog.processors.JSONRenderer(),
        structlog.dev.ConsoleRenderer(colors=False),
    ],
)
def test_budget_exceeded_fixed_output_is_renderable(chain_logger, renderer) -> None:
    """JSONとconsoleでも予算超過時は元のeventやpayloadを出さない。"""
    capture = LogCapture()
    rules = LogPolicyRules(LogPolicy.PROCESS_RUNTIME, frozenset({"payload"}))
    chain_logger(rules, capture).info(
        "synthetic-private-event",
        payload=["synthetic-private-value"] * MAX_ITEMS_PER_LOG_EVENT,
    )
    output = capture.entries[0]
    # 出力時刻は実行ごとに変わるため、形式を確認して本文の期待値から分ける。
    assert datetime.fromisoformat(output.pop("timestamp")).tzinfo is not None
    assert output == {
        "event": "log_policy_replaced",
        "level": "info",
        "log_policy": "process_runtime",
        "_policy_replaced_reason": "value_count_exceeded",
        "log_level": "info",
    }
    rendered = renderer(None, "info", dict(output))
    assert "synthetic-private" not in rendered
    assert "log_policy_replaced" in rendered
