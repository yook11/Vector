"""実際のロガー生成口からJSON標準出力までの振る舞い。"""

import json
from collections.abc import Iterator
from datetime import datetime

import pytest
import structlog

from app.log_policy import LogPolicy, LogPolicyRules
from app.log_policy.runtime import create_policy_json_logger

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_log_context() -> Iterator[None]:
    """他のテストのログコンテキストを持ち込まず、終了後は元の状態へ戻す。"""
    original = structlog.contextvars.get_contextvars()
    structlog.contextvars.clear_contextvars()
    try:
        yield
    finally:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(**original)


def test_inherited_policy_is_applied_to_the_final_json_log(capsys) -> None:
    """継承したポリシーを持つロガーは、共通情報・bind・ログ引数を合わせた一件のJSONを規則どおり出力する。"""
    common_rules = LogPolicyRules(
        policy=LogPolicy.INFRASTRUCTURE,
        allow=frozenset({"request_id", "service", "connection_url", "message"}),
        deny=frozenset({"private_note"}),
        mask=frozenset({"private_text"}),
        sanitize=frozenset({"connection_url"}),
    )
    rules = common_rules.extend(
        allow=common_rules.allow | {"operation", "upstream_message", "reference"},
        sanitize=frozenset({"upstream_message"}),
    )
    logger = create_policy_json_logger("test.logging", rules).bind(
        service="catalog",
        connection_url=(
            "https://user:synthetic@example.com/path?note=eyJabc.eyJdef.signature"
        ),
        private_note="PRIVATE_NOTE",
    )

    with structlog.contextvars.bound_contextvars(request_id="request-001"):
        logger.info(
            "upstream_checked",
            operation="healthcheck",
            message="before private_text='synthetic private' after",
            upstream_message="got eyJabc.eyJdef.signature failed",
            reference="https://docs:synthetic@example.com/reference",
            extra_field="unregistered",
        )

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    # 出力時刻は実行ごとに変わるため、形式を確認して本文の期待値から分ける。
    assert datetime.fromisoformat(entry.pop("timestamp")).tzinfo is not None
    assert entry == {
        "event": "upstream_checked",
        "level": "info",
        "log_policy": "infrastructure",
        "request_id": "request-001",
        "service": "catalog",
        "operation": "healthcheck",
        "connection_url": "https://***@example.com/path?note=eyJ***",
        "message": "before private_text=*** after",
        "upstream_message": "got eyJ*** failed",
        "reference": "https://***@example.com/reference",
        "_denied_keys": ["private_note"],
        "_unregistered_count": 1,
    }
