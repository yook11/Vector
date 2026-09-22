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
    CompletionBackfillSettings,
    CurationBackfillSettings,
    EmbeddingBackfillSettings,
)

handler = importlib.import_module("app.lambda_handlers.backfill.handler")
pytestmark = pytest.mark.unit
SETTINGS = [
    ("curation", "curations", CurationBackfillSettings),
    ("assessment", "assessments", AssessmentBackfillSettings),
    ("embedding", "embeddings", EmbeddingBackfillSettings),
    ("completion", "completions", CompletionBackfillSettings),
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


@pytest.mark.parametrize("stage,plural,settings_type", SETTINGS)
def test_stage_defaults_to_enabled_without_other_stage_settings(
    monkeypatch, stage, plural, settings_type
):
    """自工程の接続設定だけでデフォルト有効になる。"""
    monkeypatch.delenv(f"BACKFILL_{plural.upper()}_ENABLED", raising=False)
    assert getattr(settings_for(settings_type, stage), f"backfill_{plural}_enabled")


@pytest.mark.parametrize("stage,plural,settings_type", SETTINGS)
def test_disabled_does_not_start_async_execution(
    monkeypatch, stage, plural, settings_type
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


@pytest.mark.parametrize("stage,plural,settings_type", SETTINGS)
def test_handler_fixes_reference_time(monkeypatch, stage, plural, settings_type):
    """入力の時刻を使わず、起動時刻を一度取得して渡す。"""
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


def test_settings_require_iam_authentication():
    """新経路のIAM認証を無効化できない。"""
    with pytest.raises(ValidationError, match="requires RDS IAM"):
        settings_for(CurationBackfillSettings, "curation", db_iam_auth=False)


def test_settings_reject_password_in_iam_url():
    """IAM接続先に固定パスワードを併記できない。"""
    with pytest.raises(ValidationError, match="contains a password"):
        CurationBackfillSettings(
            env="production",
            aws_region="ap-northeast-1",
            database_url="postgresql+asyncpg://user:secret@database.invalid/db?sslmode=require",
            sqs_article_curation_queue_url="https://sqs.invalid/curation",
        )


def test_production_settings_require_tls():
    """productionのDB接続先にはTLS指定を必須にする。"""
    with pytest.raises(ValidationError, match="TLS sslmode"):
        CurationBackfillSettings(
            env="production",
            aws_region="ap-northeast-1",
            database_url="postgresql+asyncpg://user@database.invalid/db",
            sqs_article_curation_queue_url="https://sqs.invalid/curation",
        )


def test_settings_reject_blank_aws_region():
    """空白だけのリージョンを接続設定として扱わない。"""
    with pytest.raises(ValidationError, match="region must not be blank"):
        CurationBackfillSettings(
            env="test",
            aws_region=" ",
            database_url="postgresql+asyncpg://user@database.invalid/db",
            sqs_article_curation_queue_url="https://sqs.invalid/curation",
        )


def test_settings_reject_blank_queue_url():
    """空白だけの送信先キューを受け付けない。"""
    with pytest.raises(ValidationError, match="queue URL must not be blank"):
        settings_for(
            CurationBackfillSettings, "curation", sqs_article_curation_queue_url=" "
        )


def test_settings_failure_propagates_without_starting_execution(monkeypatch):
    """設定失敗は非同期処理を開始せず元の例外を伝える。"""
    error = RuntimeError("settings")
    load = Mock(side_effect=error)
    run = AsyncMock()
    monkeypatch.setattr(handler, "CurationBackfillSettings", load)
    monkeypatch.setattr(handler, "run_backfill", run)
    with pytest.raises(RuntimeError) as caught:
        handler.curation_handler({}, None)
    assert caught.value is error
    run.assert_not_called()


def test_execution_failure_propagates_without_retry(monkeypatch):
    """非同期実行の失敗を再試行せず元の例外を伝える。"""
    error = RuntimeError("execution")
    monkeypatch.setattr(
        handler,
        "CurationBackfillSettings",
        lambda: settings_for(CurationBackfillSettings, "curation"),
    )
    run = AsyncMock(side_effect=error)
    monkeypatch.setattr(handler, "run_backfill", run)
    with pytest.raises(RuntimeError) as caught:
        handler.curation_handler({}, None)
    assert caught.value is error
    run.assert_awaited_once()


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
