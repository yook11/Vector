"""manifestと運用CLIの入力境界を検証する。"""

import hashlib
import json

import pytest
from pydantic import ValidationError

from scripts import db_role_runner
from scripts.db_role_runner import RoleContractError, RoleManifest, load_manifest


def test_manifest_rejects_sql_in_role_name():
    """ロール名からSQLを注入できない。"""
    with pytest.raises(ValidationError):
        RoleManifest(protocol_version=1, roles=['vector_attack"; DROP ROLE vector; --'])


def test_manifest_rejects_existing_shared_role():
    """共用アプリロールを管理対象に取り込めない。"""
    with pytest.raises(ValidationError):
        RoleManifest(protocol_version=1, roles=["vector_app"])


def test_manifest_digest_binds_approved_content(tmp_path):
    """承認されたmanifestと違う内容は適用しない。"""
    path = tmp_path / "db_roles.json"
    path.write_text('{"protocol_version":1,"roles":["vector_outbox_relay"]}')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text('{"protocol_version":1,"roles":["vector_other"]}')
    with pytest.raises(RoleContractError, match="manifest_digest_mismatch"):
        load_manifest(path, digest)


def test_cli_failure_never_prints_exception_secrets(monkeypatch, capsys):
    """設定エラーに秘密が含まれてもCLI出力には持ち出さない。"""

    def fail():
        raise ValueError("secret-password-in-server-error")

    monkeypatch.setattr(db_role_runner, "RoleRunnerSettings", fail)
    assert db_role_runner.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {
        "result": "failed",
        "reason": "role_management_failed",
    }


def test_production_connection_verifies_hostname_and_keeps_password_private(
    monkeypatch, capsys
):
    """実CAで接続先を検証しpasswordは接続引数にだけ渡す。"""
    import ssl
    from unittest.mock import AsyncMock

    password = "only-the-database-driver-may-receive-this"
    digest = hashlib.sha256(
        (db_role_runner.ROOT / "db_roles.json").read_bytes()
    ).hexdigest()
    settings = db_role_runner.RoleRunnerSettings(
        db_admin_host="database.example.invalid",
        db_admin_password=password,
        db_roles_release_sha="a" * 40,
        db_roles_manifest_sha256=digest,
    )
    connect = AsyncMock(side_effect=RuntimeError(password))
    monkeypatch.setattr(db_role_runner, "RoleRunnerSettings", lambda: settings)
    monkeypatch.setattr(db_role_runner.asyncpg, "connect", connect)

    assert db_role_runner.main() == 1

    connect.assert_awaited_once()
    parameters = connect.call_args.kwargs
    assert parameters["host"] == "database.example.invalid"
    assert parameters["user"] == "vector_master"
    assert parameters["database"] == "vector"
    assert parameters["password"] == password
    assert parameters["ssl"].verify_mode == ssl.CERT_REQUIRED
    assert parameters["ssl"].check_hostname is True
    assert parameters["ssl"].cert_store_stats()["x509_ca"] > 0
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {
        "result": "failed",
        "reason": "role_management_failed",
    }
