"""共通DB基盤が失敗時にも専用環境を回収することを検証する。"""

import json

import pytest

from local_tests import database as module


@pytest.mark.parametrize("failure", ["startup", "auth", "migration", "test"])
def test_failure_always_removes_its_own_compose_project(monkeypatch, failure):
    """セットアップの途中とテスト本体の例外で同じ専用プロジェクトを削除する。"""
    calls = []
    config = {
        "services": {
            "db-test": {
                "environment": {
                    "POSTGRES_USER": "vector",
                    "POSTGRES_DB": "vector",
                    "POSTGRES_PASSWORD": "test",
                    "POSTGRES_APP_PASSWORD": "app-test",
                    "POSTGRES_AUTH_PASSWORD": "auth-test",
                    "POSTGRES_COLLECT_PASSWORD": "collect-test",
                }
            }
        }
    }

    def run(args, **kwargs):
        calls.append(args)
        if "config" in args:
            return json.dumps(config)
        if "up" in args and failure == "startup":
            raise RuntimeError("injected failure")
        if "port" in args:
            return "127.0.0.1:15432"
        if "alembic" in args and failure == "migration":
            raise RuntimeError("injected failure")
        return ""

    async def prepare(database):
        return None

    def auth(*args):
        if failure == "auth":
            raise RuntimeError("injected failure")

    monkeypatch.setattr(module, "_run", run)
    monkeypatch.setattr(module, "_prepare_auth", prepare)
    monkeypatch.setattr(module, "_verify_template", prepare)
    monkeypatch.setattr(module, "_migrate_auth", auth)
    with pytest.raises(RuntimeError, match="injected failure"):
        with module.migrated_database():
            raise RuntimeError("injected failure")
    startup = next(args for args in calls if "up" in args)
    cleanup = calls[-1]
    assert cleanup[-3:] == ["down", "-v", "--remove-orphans"]
    project = startup[startup.index("-p") + 1]
    assert project.startswith("vector-test-system-")
    assert cleanup[cleanup.index("-p") + 1] == project
    assert cleanup[cleanup.index("--env-file") + 1] == "/dev/null"


def test_case_database_uses_app_credentials_without_changing_target():
    """各利用者の接続先は同じ隔離DBで、管理者認証へ切り替わらない。"""
    from sqlalchemy.engine import make_url

    database = module.SystemDatabase(
        15432, {"vector_app": "test@password"}, "system_test"
    )
    url = make_url(database.url("vector_app", sqlalchemy=True))
    assert (url.host, url.port, url.database, url.username) == (
        "127.0.0.1",
        15432,
        "system_test",
        "vector_app",
    )
    assert url.password == "test@password"
    assert "test@password" not in repr(database)
