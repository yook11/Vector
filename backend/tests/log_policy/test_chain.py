"""JSON と console の renderer で、秘匿後の値を整形しても原文が戻らない。"""

from __future__ import annotations

import json
from functools import partial

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
    "?X-Amz-Signature=synthetic&DBUser=app "
    "got eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.abc failed "
    "password='synthetic private value' host=db"
)
_DETAIL_FRAGMENTS = (
    "synthetic private value",
    _GEMINI_KEY,
    "AKIAIOSFODNN7EXAMPLE",
    "user:secret@",
    "X-Amz-Signature=synthetic",
    "eyJhbGciOiJIUzI1NiJ9",
)


def _emit_redacted_failure(configure_chain) -> dict:
    """通常の logger 呼び出しをチェーン末尾の LogCapture で受け取る。"""
    capture = configure_chain()
    logger = policy_logger("test", _RULES)
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


def test_json_and_console_keep_the_same_redaction(configure_chain) -> None:
    """JSON と console のどちらでも、禁止値・各形式の認証情報・traceback は戻らない。"""
    entry = _emit_redacted_failure(configure_chain)
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
        "?X-Amz-Signature=[redacted:aws_signed_query]&DBUser=app "
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

    processors = build_processors(structlog.dev.ConsoleRenderer(colors=False))
    entry = {"event": "completed", "level": {"invalid": 1}}
    for processor in processors[:-1]:
        entry = processor(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()), "info", entry
        )

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
    processors = build_processors(renderer)
    for processor in processors[:-1]:
        entry = processor(
            PolicyLogger(BASE_LOG_RULES, structlog.ReturnLogger()), "info", entry
        )

    assert entry["event"] == {
        "message": "token=[redacted:credential]",
        "count": 3,
    }
    rendered = processors[-1](None, "info", dict(entry))
    assert _SECRET not in rendered
    assert "synthetic" not in rendered


def test_declared_logger_uses_globally_configured_json_renderer(
    configure_chain,
) -> None:
    """設定前に宣言したロガーも共通設定のJSONで秘匿済みのログを出す。"""
    from app.log_policy import build_processors, create_policy_logger

    logger = policy_logger("test", _RULES)
    configure_chain()
    structlog.configure(
        logger_factory=partial(
            create_policy_logger, output_logger_factory=structlog.ReturnLoggerFactory()
        ),
        processors=build_processors(structlog.processors.JSONRenderer()),
    )
    rendered = logger.info("completed", reason=f"token={_SECRET}")
    assert json.loads(rendered)["reason"] == "token=[redacted:credential]"
    assert _SECRET not in rendered


def test_zero_value_is_preserved_in_json_output(configure_chain) -> None:
    """許可された項目の0は未取得として除外せず、JSONへ数値のまま出力する。"""
    from app.log_policy import build_processors, create_policy_logger

    configure_chain()
    structlog.configure(
        logger_factory=partial(
            create_policy_logger, output_logger_factory=structlog.ReturnLoggerFactory()
        ),
        processors=build_processors(structlog.processors.JSONRenderer()),
    )
    logger = policy_logger("test", _RULES)

    log_entry = json.loads(logger.info("completed", source_id=0))

    assert type(log_entry["source_id"]) is int
    assert log_entry["source_id"] == 0


def test_declared_logger_uses_globally_configured_console_renderer(
    configure_chain,
) -> None:
    """設定前に宣言したロガーも共通設定のconsoleで秘匿済みのログを出す。"""
    from app.log_policy import build_processors, create_policy_logger

    logger = policy_logger("test", _RULES)
    configure_chain()
    structlog.configure(
        logger_factory=partial(
            create_policy_logger, output_logger_factory=structlog.ReturnLoggerFactory()
        ),
        processors=build_processors(structlog.dev.ConsoleRenderer(colors=False)),
    )
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
    configure_chain, renderer
) -> None:
    """JSONとconsoleの実チェーンでも超過項目の原文を出さず正常なeventを残す。"""
    rules = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({"payload"}))
    capture = configure_chain()
    policy_logger("test", rules).info(
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
def test_budget_exceeded_fixed_output_is_renderable(configure_chain, renderer) -> None:
    """JSONとconsoleでも予算超過時は元のeventやpayloadを出さない。"""
    capture = configure_chain()
    rules = LogPolicyRules(LogPolicy.INFRASTRUCTURE, frozenset({"payload"}))
    policy_logger("test", rules).info(
        "synthetic-private-event",
        payload=["synthetic-private-value"] * MAX_ITEMS_PER_LOG_EVENT,
    )
    output = capture.entries[0]
    assert output == {
        "event": "log_policy_budget_exceeded",
        "_policy_limited": True,
        "_policy_limit_reason": "value_count",
        "log_level": "info",
    }
    rendered = renderer(None, "info", dict(output))
    assert "synthetic-private" not in rendered
    assert "log_policy_budget_exceeded" in rendered
