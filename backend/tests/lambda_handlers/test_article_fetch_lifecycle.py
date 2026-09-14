"""記事取得の起動・資源解放と、補完への配線を確認する。"""

import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.lambda_handlers import article_fetch_lifecycle as lifecycle
from app.lambda_handlers.completion import composition
from app.lambda_handlers.completion.settings import CompletionConsumerSettings

pytestmark = pytest.mark.unit


@pytest.fixture
def resources(monkeypatch):
    rds = Mock()
    engine = SimpleNamespace(dispose=AsyncMock())
    session = Mock()
    session.create_client.return_value = rds
    monkeypatch.setattr(lifecycle, "Session", Mock(return_value=session))
    create_engine = Mock(return_value=engine)
    build_consumer = Mock(side_effect=lambda **kwargs: object())
    logger = Mock()
    recorder = lifecycle.ArticleFetchLifecycleRecorder(logger, operation="completion")
    arguments = dict(
        aws_region="ap-northeast-1",
        database_url="postgresql+asyncpg://vector_collect@db.invalid/vector?sslmode=require",
        create_engine=create_engine,
        build_consumer=build_consumer,
        failure_recorder=recorder,
    )
    return SimpleNamespace(
        rds=rds,
        engine=engine,
        create_engine=create_engine,
        build_consumer=build_consumer,
        logger=logger,
        session=session,
        open=lambda: lifecycle.open_article_fetch_consumer(**arguments),
    )


@pytest.mark.asyncio
async def test_normal_exit_releases_database_resources(resources):
    """記事取得工程が終了したら、その呼び出しで生成したDB資源を解放する。"""
    async with resources.open():
        resources.rds.close.assert_not_called()
        resources.engine.dispose.assert_not_awaited()
    resources.rds.close.assert_called_once()
    resources.engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("proxy", [None, "http://public.invalid"])
async def test_invalid_proxy_fails_before_resources_are_acquired(
    resources, monkeypatch, proxy
):
    """プロキシ未設定・不正は記事処理の前に起動を失敗させる。"""
    monkeypatch.delenv("EGRESS_PROXY_URL", raising=False)
    if proxy is not None:
        monkeypatch.setenv("EGRESS_PROXY_URL", proxy)
    with pytest.raises(ValidationError):
        async with resources.open():
            pytest.fail("不正設定では起動しない")
    resources.session.create_client.assert_not_called()
    resources.build_consumer.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_phase", ["create_engine", "build_consumer"])
async def test_initialization_failure_releases_acquired_resources(
    resources, failed_phase
):
    """初期化途中の失敗では取得済み資源だけを解放し、元の原因を返す。"""
    original = RuntimeError("private initialization details")
    getattr(resources, failed_phase).side_effect = original
    with pytest.raises(RuntimeError) as caught:
        async with resources.open():
            pytest.fail("初期化失敗では貸し出さない")
    assert caught.value is original
    resources.rds.close.assert_called_once()
    if failed_phase == "build_consumer":
        resources.engine.dispose.assert_awaited_once()
    else:
        resources.engine.dispose.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_cancellation_releases_resources(resources):
    """利用中の外部キャンセルは伝播し、取得済み資源を解放する。"""
    entered = asyncio.Event()

    async def use():
        async with resources.open():
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(use())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    resources.rds.close.assert_called_once()
    resources.engine.dispose.assert_awaited_once()
    resources.logger.warning.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original", [None, RuntimeError("original"), asyncio.CancelledError()]
)
async def test_cleanup_and_logging_failures_preserve_outcome(resources, original):
    """解放と診断の通常例外は利用結果を置換せず、残りの資源も解放する。"""
    resources.engine.dispose.side_effect = RuntimeError("private cleanup")
    resources.rds.close.side_effect = RuntimeError("private cleanup")
    resources.logger.warning.side_effect = RuntimeError("private logging")

    async def use():
        async with resources.open():
            if original is not None:
                raise original
        return "completed"

    if original is None:
        assert await use() == "completed"
    else:
        with pytest.raises(type(original)) as caught:
            await use()
        assert caught.value is original
    resources.rds.close.assert_called_once()
    resources.engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_initialization_diagnostic_does_not_expose_exception_text(resources):
    """初期化診断は例外型だけを出力し、機密を含み得る自由文を出さない。"""
    resources.build_consumer.side_effect = RuntimeError("private initialization")
    with pytest.raises(RuntimeError):
        async with resources.open():
            pass
    call = resources.logger.warning.call_args
    assert call.kwargs["error_class"] == "builtins.RuntimeError"
    assert "private" not in repr(call)


@pytest.mark.asyncio
async def test_sqs_initialization_failure_releases_fetch_resources(
    resources, monkeypatch
):
    """配送資源を用意できなくても、先に生成した記事取得の資源を残さない。"""
    monkeypatch.setattr(
        composition, "create_article_fetch_engine", resources.create_engine
    )
    original = RuntimeError("SQS setup failed")
    session = Mock()
    session.create_client.side_effect = original
    monkeypatch.setattr(composition, "Session", Mock(return_value=session))
    monkeypatch.setattr(composition, "logger", resources.logger)
    settings = CompletionConsumerSettings(
        database_url="postgresql+asyncpg://vector_collect@db.invalid/vector?sslmode=require",
        aws_region="ap-northeast-1",
    )
    with pytest.raises(RuntimeError) as caught:
        async with composition.open_completion_resources(settings):
            pytest.fail("配送資源がなければ起動しない")
    assert caught.value is original
    resources.engine.dispose.assert_awaited_once()
    resources.rds.close.assert_called_once()


@pytest.mark.asyncio
async def test_sqs_cleanup_failure_preserves_result_and_releases_database(
    resources, monkeypatch
):
    """配送側の解放・診断が失敗しても利用結果を保ち、DB資源も解放する。"""
    monkeypatch.setattr(
        composition, "create_article_fetch_engine", resources.create_engine
    )
    sqs = Mock()
    sqs.close.side_effect = RuntimeError("private cleanup")
    session = Mock()
    session.create_client.return_value = sqs
    monkeypatch.setattr(composition, "Session", Mock(return_value=session))
    monkeypatch.setattr(composition, "logger", resources.logger)
    resources.logger.warning.side_effect = RuntimeError("private logging")
    settings = CompletionConsumerSettings(
        database_url="postgresql+asyncpg://vector_collect@db.invalid/vector?sslmode=require",
        aws_region="ap-northeast-1",
    )
    original = RuntimeError("consumer result")
    with pytest.raises(RuntimeError) as caught:
        async with composition.open_completion_resources(settings):
            raise original
    assert caught.value is original
    sqs.close.assert_called_once()
    resources.engine.dispose.assert_awaited_once()
    resources.rds.close.assert_called_once()


def test_completion_starts_without_unrelated_application_settings():
    """最小の環境で実Consumerと実クライアントを生成・解放できる。"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import asyncio
import importlib.abc
import sys

class RejectApplicationSettings(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "app.config":
            raise AssertionError("completion must not import application settings")

sys.meta_path.insert(0, RejectApplicationSettings())
from app.lambda_handlers.completion.composition import open_completion_resources
from app.lambda_handlers.completion.handler import handler
from app.lambda_handlers.completion.settings import CompletionConsumerSettings

async def run():
    async with open_completion_resources(CompletionConsumerSettings()) as resources:
        assert callable(resources.consumer.consume)
        assert callable(resources.sqs_client.change_message_visibility)
asyncio.run(run())
assert handler({"Records": []}, None) == {"batchItemFailures": []}
assert "app.config" not in sys.modules
assert not any(m.startswith("app.queue") for m in sys.modules)
""",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env={
            "ENV": "production",
            "DATABASE_URL": "postgresql+asyncpg://vector_collect@db.invalid/vector?sslmode=verify-full",
            "AWS_REGION": "ap-northeast-1",
            "EGRESS_PROXY_URL": "http://proxy.vector.internal:3128",
            "AWS_ACCESS_KEY_ID": "test-only-access-key",
            "AWS_SECRET_ACCESS_KEY": "test-only-secret-key",
            "AWS_EC2_METADATA_DISABLED": "true",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
