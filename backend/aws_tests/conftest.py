"""AWS出力を明示した場合だけ、準備済み試験環境への接続を許可する。"""

import json
from pathlib import Path

import pytest

from aws_tests.embedding.runtime import EmbeddingRuntime


def pytest_addoption(parser):
    group = parser.getgroup("aws-smoke")
    group.addoption("--aws-smoke-outputs", help="試験環境のterraform output -json")
    group.addoption("--aws-account-config", help="期待アカウントのローカルaccount.json")
    group.addoption("--aws-runner-profile", default="vector-test-runner")


@pytest.fixture(scope="session")
def embedding_runtime(request):
    outputs_path = request.config.getoption("--aws-smoke-outputs")
    account_path = request.config.getoption("--aws-account-config")
    if not outputs_path or not account_path:
        raise pytest.UsageError(
            "--aws-smoke-outputs と --aws-account-config が必要です"
        )
    outputs = {
        k: v["value"] for k, v in json.loads(Path(outputs_path).read_text()).items()
    }
    account = json.loads(Path(account_path).read_text())
    runtime = EmbeddingRuntime(
        outputs,
        account["expected_account_id"],
        request.config.getoption("--aws-runner-profile"),
    )
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.fixture
def aws_embedding(embedding_runtime, record_property):
    start = len(embedding_runtime.evidence)
    record_property("run_id", embedding_runtime.run["run_id"])
    record_property("source_revision", embedding_runtime.run["source_revision"])
    record_property("backend_image", embedding_runtime.run["backend_image"])
    try:
        yield embedding_runtime
    finally:
        record_property("evidence", json.dumps(embedding_runtime.evidence[start:]))
