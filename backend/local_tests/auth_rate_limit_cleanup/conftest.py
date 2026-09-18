"""専用ロールの実DB接続でLambdaを呼び出す。"""

import asyncio
import time
from importlib import import_module
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from tests.iam_fixtures import inject_test_db_signer

NOW_MS = 1_800_000_000_000


@pytest.fixture
def cleanup(system_database, monkeypatch):
    module = import_module("app.lambda_handlers.auth_rate_limit_cleanup.handler")
    url = inject_test_db_signer(
        monkeypatch,
        system_database.url("vector_auth_rate_limit_cleanup", sqlalchemy=True),
        resources_module=module,
    )
    for key, value in {
        "ENV": "test",
        "DATABASE_URL": url,
        "DB_IAM_AUTH": "true",
        "AWS_REGION": "ap-northeast-1",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        module,
        "time",
        SimpleNamespace(time_ns=lambda: NOW_MS * 1_000_000, monotonic=time.monotonic),
    )
    engines, disposed, clients = [], [], []
    client_factory = module.Session.return_value.create_client
    create_client = client_factory.side_effect

    def observe_client(*args, **kwargs):
        client = create_client(*args, **kwargs)
        clients.append(client)
        return client

    client_factory.side_effect = observe_client
    create_engine = module.create_auth_rate_limit_cleanup_engine
    listeners = []

    def observe_engine(*args, **kwargs):
        engine = create_engine(*args, **kwargs)
        engines.append(engine)
        event.listen(
            engine.sync_engine, "engine_disposed", lambda e: disposed.append(e)
        )
        for name, listener in listeners:
            event.listen(engine.sync_engine, name, listener)
        return engine

    monkeypatch.setattr(module, "create_auth_rate_limit_cleanup_engine", observe_engine)

    async def invoke(payload=None):
        await asyncio.to_thread(
            module.handler,
            {} if payload is None else payload,
            SimpleNamespace(aws_request_id="cleanup-test"),
        )

    return SimpleNamespace(
        invoke=invoke,
        module=module,
        listeners=listeners,
        engines=engines,
        disposed=disposed,
        clients=clients,
    )


@pytest.fixture
async def counters(system_database):
    async with system_database.connect("vector") as connection:
        await connection.executemany(
            'INSERT INTO auth."rateLimit" ("id", "key", "count", "lastRequest") '
            "VALUES (gen_random_uuid(), $1, 1, $2)",
            [
                ("expired", NOW_MS - 600_001),
                ("boundary", NOW_MS - 600_000),
                ("recent", NOW_MS),
            ],
        )
    return system_database
