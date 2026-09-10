from __future__ import annotations

import subprocess
import sys
from importlib import import_module
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name", ["outbox_relay", "embedding"])
def test_existing_handler_path_resolves_after_loading_implementation(name):
    """実装モジュールの読み込み後も既存の起動パスが関数を指す。"""
    module_name, function_name = f"app.lambda_handlers.{name}.handler".rsplit(".", 1)
    implementation = import_module(f"{module_name}.handler")
    entry = getattr(import_module(module_name), function_name)
    assert callable(entry)
    assert entry is implementation.handler


def test_handler_loads_with_only_database_and_queue_settings() -> None:
    """別プロセスでアプリ全体の設定や.envに依存しない起動を確認する。"""
    backend_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.lambda_handlers.outbox_relay import handler; "
            "from app.lambda_handlers.outbox_relay.settings "
            "import OutboxRelaySettings; "
            "from importlib import import_module; "
            "assert callable(handler); "
            "module, name = 'app.lambda_handlers.outbox_relay.handler'.rsplit('.', 1); "
            "assert getattr(import_module(module), name) is handler; "
            "assert import_module(module + '.handler').handler is handler; "
            "import sys; "
            "OutboxRelaySettings(); "
            "assert 'app.config' not in sys.modules; "
            "assert 'app.main' not in sys.modules; "
            "assert not any(m.startswith('app.queue') for m in sys.modules)",
        ],
        cwd=backend_root,
        env={
            "ENV": "production",
            "DATABASE_URL": (
                "postgresql+asyncpg://vector_app@database.invalid/vector"
                "?sslmode=verify-full"
            ),
            "DB_IAM_AUTH": "true",
            "AWS_REGION": "ap-northeast-1",
            "MIGRATION_DATABASE_URL": "invalid-unused-url",
            "AUTH_RETENTION_DATABASE_URL": "invalid-unused-url",
            **{
                f"SQS_ARTICLE_{stage}_QUEUE_URL": f"https://sqs.invalid/{stage}"
                for stage in ("COMPLETION", "CURATION", "ASSESSMENT", "EMBEDDING")
            },
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
