"""AWS migration用Terraform・DB切替の契約。"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.unit


def _text(path: str) -> str:
    return (_ROOT / path).read_text(encoding="utf-8")


def test_local_alembic_container_does_not_receive_application_env_file() -> None:
    compose = _text("docker-compose.yml")
    migration = compose.split("  db-init-alembic:", maxsplit=1)[1].split(
        "  # --- Backend", maxsplit=1
    )[0]

    assert "env_file:" not in migration
    assert "MIGRATION_DATABASE_URL:" in migration
    assert "BFF_JWT_SIGNING_SECRET" not in migration


def test_db_owner_switch_removes_master_membership_before_iam_grant() -> None:
    sql = _text("infra/aws/db-provision.sql")
    assert "\\set ON_ERROR_STOP on" in sql
    ordered = [
        "BEGIN;",
        "GRANT vector TO vector_master;",
        "ALTER DATABASE vector OWNER TO vector;",
        "ALTER SCHEMA public OWNER TO vector;",
        "REVOKE vector FROM vector_master;",
        "GRANT rds_iam TO vector;",
        "ALTER ROLE vector PASSWORD NULL;",
        "COMMIT;",
    ]
    positions = [sql.index(statement) for statement in ordered]
    assert positions == sorted(positions)
