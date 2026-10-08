"""Lambda入口のJSONログと既存EMF出力の境界を検証する。"""

import asyncio
import json
from unittest.mock import Mock

import pytest
import structlog

from app.cloudwatch.emf import emit_metric
from app.lambda_handlers.logging import setup_lambda_logging

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def restore_logging():
    configure = structlog.configure
    original = structlog.get_config().copy()
    yield
    configure(**original)


def test_json_output_and_emf_remain_separate(capsys):
    """繰り返す初期化でログを重複させず、EMFの最上位構造を保つ。"""
    logger = structlog.get_logger("before_setup")
    for index in range(2):
        setup_lambda_logging()
        logger.info("embedding_message_completed", message_id=f"m-{index}")
    emit_metric(
        "processing_outcome", dimensions={"stage": "embedding"}, value=1, unit="Count"
    )
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(records) == 3
    assert [r["message_id"] for r in records[:2]] == ["m-0", "m-1"]
    assert all(
        r["level"] == "info" and r["timestamp"].endswith("Z") for r in records[:2]
    )
    assert records[2]["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "Vector/Pipeline"
    assert "event" not in records[2]


def test_logging_setup_failure_is_not_raised(monkeypatch):
    """ログ設定の通常障害を呼び出し元へ伝えない。"""
    monkeypatch.setattr(structlog, "configure", Mock(side_effect=RuntimeError("log")))

    assert setup_lambda_logging() is None


def test_logging_setup_does_not_swallow_cancellation(monkeypatch):
    """キャンセルはログ初期化中でも通常障害として扱わない。"""
    monkeypatch.setattr(
        structlog, "configure", Mock(side_effect=asyncio.CancelledError())
    )
    with pytest.raises(asyncio.CancelledError):
        setup_lambda_logging()
