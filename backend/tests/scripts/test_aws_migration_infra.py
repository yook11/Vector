"""AWS migration用Terraform・DB切替の契約。"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.unit


def _text(path: str) -> str:
    return (_ROOT / path).read_text(encoding="utf-8")


def test_migration_task_is_passwordless_and_has_no_application_secrets() -> None:
    ecs = _text("infra/aws/ecs.tf")
    migration = ecs.split('resource "aws_ecs_task_definition" "migration"', maxsplit=1)[
        1
    ].split('resource "aws_ecs_service"', maxsplit=1)[0]

    assert (
        'command    = ["python", "-m", "scripts.run_production_migration"]' in migration
    )
    assert '{ name = "ENV", value = "production" }' in migration
    assert '{ name = "DB_IAM_AUTH", value = "true" }' in migration
    assert "MIGRATION_DATABASE_URL" in migration
    assert "secrets = []" in migration
    assert "BFF_JWT_SIGNING_SECRET" not in migration
    assert "SSM" not in migration


def test_local_alembic_container_does_not_receive_application_env_file() -> None:
    compose = _text("docker-compose.yml")
    migration = compose.split("  db-init-alembic:", maxsplit=1)[1].split(
        "  # --- Backend", maxsplit=1
    )[0]

    assert "env_file:" not in migration
    assert "MIGRATION_DATABASE_URL:" in migration
    assert "BFF_JWT_SIGNING_SECRET" not in migration


def test_migration_network_has_only_rds_endpoints_and_s3_egress() -> None:
    security_groups = _text("infra/aws/security_groups.tf")
    names = {
        '"migration_to_rds"',
        '"migration_to_endpoints"',
        '"migration_to_s3"',
    }
    assert all(name in security_groups for name in names)
    assert '"migration_to_proxy"' not in security_groups
    assert '"migration_to_valkey"' not in security_groups
    assert "aws_security_group.migration_endpoints.id" in security_groups

    endpoints = _text("infra/aws/endpoints.tf")
    assert 'contains(["ecr.api", "ecr.dkr", "logs"], each.value)' in endpoints
    assert "[aws_security_group.migration_endpoints.id] : []" in endpoints

    network = _text("infra/aws/network.tf")
    association = network.split(
        'resource "aws_route_table_association" "migration"', maxsplit=1
    )[1].split("}", maxsplit=1)[0]
    assert "aws_route_table.app.id" in association


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
