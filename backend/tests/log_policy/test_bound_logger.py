"""ログ障害の捕捉を、共通ラッパーの公開メソッドで確認する。"""

import asyncio
from unittest.mock import Mock

import pytest
import structlog

from app.log_policy.bound_logger import ApplicationBoundLogger

pytestmark = pytest.mark.unit


@pytest.fixture
def output():
    stream = Mock()
    logger = structlog.wrap_logger(
        structlog.WriteLoggerFactory(file=stream)(),
        processors=[structlog.processors.JSONRenderer()],
        wrapper_class=ApplicationBoundLogger,
        context_class=dict,
        cache_logger_on_first_use=False,
    )
    return logger, stream


def test_bound_logger_does_not_propagate_or_retry_output_error(output) -> None:
    """ロギングに失敗した時に、呼び出し元に例外を返さない。"""
    logger, stream = output
    stream.write.side_effect = OSError("output unavailable")

    logger.bind(stage="assessment").warning("processing_failed")

    stream.write.assert_called_once()


def test_rendering_error_does_not_escape_or_output_raw_data(output) -> None:
    """ログに渡したデータのJSON変換に失敗しても、呼び出し元へ例外を伝播させない。"""
    logger, stream = output
    recursive_value = {}
    recursive_value["self"] = recursive_value

    logger.info("processing_completed", detail=recursive_value)

    stream.write.assert_not_called()


@pytest.mark.parametrize(
    "interruption",
    [
        pytest.param(KeyboardInterrupt(), id="keyboard-interrupt"),
        pytest.param(SystemExit(), id="system-exit"),
        pytest.param(asyncio.CancelledError(), id="task-cancelled"),
    ],
)
def test_interruptions_outside_exception_propagate(output, interruption) -> None:
    """Exceptionに含まれない中断・終了・キャンセルの例外は、呼び出し元へ伝播させる。"""
    logger, stream = output
    stream.write.side_effect = interruption

    with pytest.raises(type(interruption)) as caught:
        logger.error("processing_failed")

    assert caught.value is interruption
