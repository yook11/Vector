"""工程別の入口が設定と起動時刻を固定して本体を起動することを確認する。"""

import importlib
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.lambda_handlers.backfill import failure_recorder
from app.lambda_handlers.backfill.settings import (
    AssessmentBackfillSettings,
    CurationBackfillSettings,
    EmbeddingBackfillSettings,
)

handler = importlib.import_module("app.lambda_handlers.backfill.handler")
pytestmark = pytest.mark.unit
STAGES = [
    ("curation", "curations", CurationBackfillSettings, "article.analyzable_created"),
    ("assessment", "assessments", AssessmentBackfillSettings, "article.curated_signal"),
    ("embedding", "embeddings", EmbeddingBackfillSettings, "article.assessed_in_scope"),
]


def settings_for(settings_type, stage, **overrides):
    return settings_type(
        env="production",
        database_url="postgresql+asyncpg://vector_app@database.invalid/vector?sslmode=require",
        aws_region="ap-northeast-1",
        **{
            f"sqs_article_{stage}_queue_url": f"https://sqs.invalid/{stage}",
            **overrides,
        },
    )


@pytest.mark.parametrize("stage,plural,settings_type,event_type", STAGES)
def test_stage_defaults_to_enabled_without_other_stage_settings(
    monkeypatch, stage, plural, settings_type, event_type
):
    """自工程の接続設定だけでデフォルト有効になる。"""
    monkeypatch.delenv(f"BACKFILL_{plural.upper()}_ENABLED", raising=False)
    assert getattr(settings_for(settings_type, stage), f"backfill_{plural}_enabled")


@pytest.mark.parametrize("stage,plural,settings_type,event_type", STAGES)
def test_disabled_does_not_start_async_execution(
    monkeypatch, stage, plural, settings_type, event_type
):
    """無効時は接続管理を含む非同期実行を開始しない。"""
    settings = settings_for(
        settings_type, stage, **{f"backfill_{plural}_enabled": False}
    )
    monkeypatch.setattr(handler, settings_type.__name__, lambda: settings)
    run = Mock(side_effect=AssertionError("must not run"))
    monkeypatch.setattr(handler.asyncio, "run", run)
    assert getattr(handler, f"{stage}_handler")({}, None) is None
    run.assert_not_called()


@pytest.mark.parametrize("stage,plural,settings_type,event_type", STAGES)
def test_handler_passes_fixed_time_and_stage_route(
    monkeypatch, stage, plural, settings_type, event_type
):
    """入力イベントによらず起動時刻と自工程の配線を一度渡す。"""
    settings = settings_for(
        settings_type, stage, **{f"backfill_{plural}_enabled": True}
    )
    monkeypatch.setattr(handler, settings_type.__name__, lambda: settings)
    now = datetime(2026, 9, 14, 12, tzinfo=UTC)
    clock = Mock(return_value=now)
    monkeypatch.setattr(handler, "utc_now", clock)
    run = AsyncMock()
    monkeypatch.setattr(handler, "run_backfill", run)

    assert (
        getattr(handler, f"{stage}_handler")(
            {"now": "ignored", "stage": "ignored"}, None
        )
        is None
    )

    clock.assert_called_once_with()
    run.assert_awaited_once()
    args = run.call_args
    assert args.args == (settings,)
    assert args.kwargs["now"] is now
    assert args.kwargs["stage"] == stage
    assert args.kwargs["operation"] is getattr(handler, f"backfill_{plural}")
    assert args.kwargs["route"].event_type == event_type
    assert args.kwargs["route"].queue_url == f"https://sqs.invalid/{stage}"


@pytest.mark.parametrize(
    "override",
    [
        {"db_iam_auth": False},
        {
            "database_url": "postgresql+asyncpg://user:secret@database.invalid/db?sslmode=require"
        },
        {"database_url": "postgresql+asyncpg://user@database.invalid/db"},
        {"aws_region": " "},
        {"sqs_article_curation_queue_url": " "},
    ],
)
def test_invalid_connection_settings_are_rejected(override):
    """IAM・TLS・空の接続設定を入口で拒否する。"""
    defaults = {
        "env": "production",
        "database_url": "postgresql+asyncpg://user@database.invalid/db?sslmode=require",
        "aws_region": "ap-northeast-1",
        "sqs_article_curation_queue_url": "https://sqs.invalid/curation",
    }
    with pytest.raises(ValidationError):
        CurationBackfillSettings(**(defaults | override))


@pytest.mark.parametrize("phase", ["settings", "execution"])
def test_handler_failure_propagates_without_retry(monkeypatch, phase):
    """設定と実行の失敗を正常終了に変換せず、そのまま伝える。"""
    error = RuntimeError("private detail")
    load = Mock(return_value=settings_for(CurationBackfillSettings, "curation"))
    run = AsyncMock()
    if phase == "settings":
        load.side_effect = error
    else:
        run.side_effect = error
    monkeypatch.setattr(handler, "CurationBackfillSettings", load)
    monkeypatch.setattr(handler, "run_backfill", run)
    with pytest.raises(RuntimeError) as caught:
        handler.curation_handler({}, None)
    assert caught.value is error
    load.assert_called_once()
    assert run.await_count == (phase == "execution")


def test_failure_log_contains_only_stage_phase_and_exception_type(monkeypatch):
    """例外本文や設定値を診断へ含めない。"""
    log = Mock()
    monkeypatch.setattr(failure_recorder, "logger", log)
    failure_recorder.record_failure("curation", "settings", RuntimeError("secret"))
    log.error.assert_called_once_with(
        "backfill_lambda_failed",
        stage="curation",
        phase="settings",
        error_class="builtins.RuntimeError",
    )


def test_logging_failure_does_not_replace_settings_failure(monkeypatch):
    """ログ障害でも設定検証の元の例外を維持する。"""
    error = RuntimeError("original")
    monkeypatch.setattr(handler, "CurationBackfillSettings", Mock(side_effect=error))
    monkeypatch.setattr(
        failure_recorder, "logger", Mock(error=Mock(side_effect=ValueError("log")))
    )
    with pytest.raises(RuntimeError) as caught:
        handler.curation_handler({}, None)
    assert caught.value is error


def test_import_and_settings_do_not_load_application_or_ai_configuration():
    """専用設定だけで入口をimportでき、旧経路やAIクライアントを初期化しない。"""
    code = """
import sys
from app.lambda_handlers import backfill
from app.lambda_handlers.backfill import settings
for stage in ['curation', 'assessment', 'embedding']:
    assert callable(getattr(backfill, f'{stage}_handler'))
    cls = getattr(settings, f'{stage.title()}BackfillSettings')
    cls(**{f'sqs_article_{stage}_queue_url': 'https://sqs.invalid/queue'})
assert 'app.config' not in sys.modules
assert not any(m.startswith(('app.queue', 'taskiq')) for m in sys.modules)
assert not any(
    m.startswith(('app.ai_providers.gemini', 'app.ai_providers.deepseek'))
    for m in sys.modules
)
"""
    result = subprocess.run(  # noqa: S603 — 固定した検証コードのみ実行する。
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[3],
        env={
            "ENV": "production",
            "DATABASE_URL": "postgresql+asyncpg://user@database.invalid/db?sslmode=require",
            "AWS_REGION": "ap-northeast-1",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
