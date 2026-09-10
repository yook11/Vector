"""Lambda入口のJSONログと既存EMF出力の境界を検証する。"""

import asyncio
import json
from importlib import import_module
from unittest.mock import AsyncMock, Mock

import pytest
import structlog

from app.cloudwatch.emf import emit_metric
from app.lambda_handlers.logging import setup_lambda_logging

entry = import_module("app.lambda_handlers.embedding.handler")
pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def restore_logging(monkeypatch):
    # 他テストのメソッド差し替えで束縛済みになったloggerを持ち越さない。
    monkeypatch.setattr(entry, "logger", structlog.get_logger(entry.__name__))
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


def test_initialization_failure_is_json_before_settings(monkeypatch, capsys):
    """設定エラーも安全な項目だけでJSON出力し、元の例外を伝える。"""
    failure = ValueError("secret-value")
    monkeypatch.setattr(entry, "EmbeddingConsumerSettings", Mock(side_effect=failure))
    run = AsyncMock()
    monkeypatch.setattr(entry, "_run_embedding", run)
    with pytest.raises(ValueError) as caught:
        entry.handler({"body": "private-input"}, None)
    assert caught.value is failure
    output = capsys.readouterr().out
    record = json.loads(output)
    assert record["event"] == "embedding_initialization_failed"
    assert record["stage"] == "settings"
    assert record["error_class"] == "builtins.ValueError"
    assert set(record) == {"event", "stage", "error_class", "level", "timestamp"}
    assert "secret-value" not in output and "private-input" not in output
    run.assert_not_called()


@pytest.mark.parametrize("logging_fails", [False, True])
def test_logging_setup_preserves_response(monkeypatch, logging_fails):
    """ログ初期化の通常障害でも確定した失敗一覧を変更しない。"""
    settings = object()
    monkeypatch.setattr(entry, "EmbeddingConsumerSettings", Mock(return_value=settings))
    failed = [{"itemIdentifier": "failed"}]
    run = AsyncMock(return_value=failed)
    monkeypatch.setattr(entry, "_run_embedding", run)
    if logging_fails:
        monkeypatch.setattr(
            structlog, "configure", Mock(side_effect=RuntimeError("log"))
        )
    assert entry.handler({"Records": []}, None) == {"batchItemFailures": failed}
    run.assert_awaited_once_with({"Records": []}, settings)


def test_logging_setup_does_not_swallow_cancellation(monkeypatch):
    """キャンセルはログ初期化中でも通常障害として扱わない。"""
    monkeypatch.setattr(
        structlog, "configure", Mock(side_effect=asyncio.CancelledError())
    )
    with pytest.raises(asyncio.CancelledError):
        setup_lambda_logging()


def test_stdout_failure_preserves_original_exception(monkeypatch):
    """実際のログ書き込み障害でも初期化時の元の例外を保持する。"""
    failure = ValueError("original")
    monkeypatch.setattr(entry, "EmbeddingConsumerSettings", Mock(side_effect=failure))
    stream = Mock()
    stream.write.side_effect = OSError("broken output")
    with monkeypatch.context() as scoped:
        scoped.setattr("sys.stdout", stream)
        with pytest.raises(ValueError) as caught:
            entry.handler({"Records": []}, None)
    assert caught.value is failure
    stream.write.assert_called_once()
