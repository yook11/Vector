from __future__ import annotations

import subprocess
import sys
from importlib import import_module
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("name", "public_name", "implementation_name"),
    [
        ("outbox_relay", "handler", "embedding_handler"),
        ("outbox_relay", "assessment_handler", "assessment_handler"),
        ("outbox_relay", "curation_handler", "curation_handler"),
        ("embedding", "handler", "handler"),
        ("curation", "handler", "handler"),
    ],
)
def test_existing_handler_path_resolves_after_loading_implementation(
    name, public_name, implementation_name
):
    """実装の改名後も、公開した起動パスが用途別の関数を指す。"""
    module_name = f"app.lambda_handlers.{name}"
    implementation = import_module(f"{module_name}.handler")
    entry = getattr(import_module(module_name), public_name)
    assert callable(entry)
    assert entry is getattr(implementation, implementation_name)


def test_handler_loads_with_only_database_and_queue_settings() -> None:
    """別プロセスでアプリ全体の設定や.envに依存しない起動を確認する。"""
    backend_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.lambda_handlers.outbox_relay import handler; "
            "from app.lambda_handlers.outbox_relay.settings "
            "import EmbeddingOutboxRelaySettings, CurationOutboxRelaySettings; "
            "from importlib import import_module; "
            "assert callable(handler); "
            "module, name = 'app.lambda_handlers.outbox_relay.handler'.rsplit('.', 1); "
            "assert getattr(import_module(module), name) is handler; "
            "assert import_module(module + '.handler').embedding_handler is handler; "
            "import sys; "
            "EmbeddingOutboxRelaySettings(); CurationOutboxRelaySettings(); "
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
            "SQS_ARTICLE_EMBEDDING_QUEUE_URL": "https://sqs.invalid/EMBEDDING",
            "SQS_ARTICLE_CURATION_QUEUE_URL": "https://sqs.invalid/CURATION",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("stage", ["embedding", "curation"])
def test_gemini_stage_loads_and_creates_client_without_application_settings(
    stage,
) -> None:
    """専用環境だけでimportとGemini生成を行い、全体設定の読み込みを禁止する。"""
    code = """
import asyncio
import importlib.abc
import sys
from unittest.mock import patch

class RejectApplicationSettings(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "app.config":
            raise AssertionError("HTTP must not load application settings")

sys.meta_path.insert(0, RejectApplicationSettings())
from app.lambda_handlers.embedding import handler
from app.lambda_handlers.embedding.settings import EmbeddingConsumerSettings
from app.ai_providers.gemini.client import open_gemini_client
from app.ai_providers.gemini.settings import GeminiConnectionSettings
from app.http import external
from pydantic import SecretStr

assert callable(handler)
EmbeddingConsumerSettings()
async def run():
    with patch.object(
        external, "_PinnedDnsTransport", wraps=external._PinnedDnsTransport
    ) as transport:
        async with open_gemini_client(
            api_key=SecretStr("test-only-key"), settings=GeminiConnectionSettings()
        ):
            assert transport.call_args.kwargs["proxy"] == "http://proxy.vector.internal:3128"
asyncio.run(run())
assert "app.config" not in sys.modules
assert "app.main" not in sys.modules
assert not any(m.startswith("app.queue") for m in sys.modules)
"""
    code = code.replace("embedding", stage).replace("Embedding", stage.title())
    result = subprocess.run(  # noqa: S603 — 固定したテストコードのみを実行する。
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        env={
            "ENV": "production",
            "DATABASE_URL": "postgresql+asyncpg://vector_app@database.invalid/vector?sslmode=verify-full",
            "DB_IAM_AUTH": "true",
            "AWS_REGION": "ap-northeast-1",
            "GEMINI_API_KEY_PARAMETER_PATH": f"/test/{stage}/gemini-key",
            "EGRESS_PROXY_URL": "http://proxy.vector.internal:3128",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("stage", ["embedding", "curation"])
def test_gemini_stage_import_does_not_validate_http_settings(stage) -> None:
    """不正なプロキシ設定もimport時には読まず、クライアント生成時に拒否する。"""
    result = subprocess.run(  # noqa: S603 — 工程名は固定のテスト引数のみ。
        [
            sys.executable,
            "-c",
            f"from app.lambda_handlers.{stage} import handler; "
            "from app.http.external import make_external_async_client; "
            "from pydantic import ValidationError; "
            "import sys; "
            "assert callable(handler); "
            "assert 'app.config' not in sys.modules; "
            "\ntry:\n    make_external_async_client()\n"
            "except ValidationError:\n    pass\n"
            "else:\n    raise AssertionError('invalid proxy accepted')\n",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env={"EGRESS_PROXY_URL": "http://untrusted.invalid"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
