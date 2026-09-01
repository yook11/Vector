"""AWS migration用bootstrap IAMの権限境界契約。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.unit


def _text(path: str) -> str:
    return (_ROOT / path).read_text(encoding="utf-8")


def test_bootstrap_pairs_dedicated_roles_with_narrow_boundaries() -> None:
    boundary = _text("infra/aws/bootstrap/boundary.tf")
    oidc = _text("infra/aws/bootstrap/oidc.tf")

    assert 'role_names = ["${var.name_prefix}-migration-task"]' in boundary
    assert 'role_names = ["${var.name_prefix}-migration-exec"]' in boundary
    assert "dbuser:*/vector" in boundary
    assert "repository/${var.name_prefix}/backend" in boundary
    assert "log-group:/ecs/${var.name_prefix}/migration" in boundary
    assert "ssmmessages:*" in boundary
    assert 'name = "db-migrate"' in oidc
    assert "environment:${var.deploy_environment}" in oidc


def test_migration_ci_role_cannot_retag_existing_tasks_or_pass_app_roles() -> None:
    oidc = _text("infra/aws/bootstrap/oidc.tf")
    migrate = oidc.split("# --- db-migrate", maxsplit=1)[1]
    register = migrate.split(
        'Sid      = "RegisterMigrationTaskDefinition"', maxsplit=1
    )[1].split('Sid      = "RunMigrationTask"', maxsplit=1)[0]
    run = migrate.split('Sid      = "RunMigrationTask"', maxsplit=1)[1].split(
        'Sid      = "TagMigrationResourcesOnlyAtCreation"', maxsplit=1
    )[0]
    stop = migrate.split('Sid      = "StopTaggedMigrationTask"', maxsplit=1)[1].split(
        'Sid    = "InspectMigrationTasks"', maxsplit=1
    )[0]

    assert 'Action   = "ecs:RegisterTaskDefinition"' in migrate
    assert 'Resource = "*"' in migrate
    assert 'Action   = "ecs:RunTask"' in migrate
    assert "task-definition/${var.name_prefix}-migration:*" in migrate
    assert migrate.count("Bool = {") == 0
    assert re.search(r'"ecs:privileged"\s*=\s*"false"', register)
    assert re.search(r'"ecs:enable-execute-command"\s*=\s*"false"', run)
    assert re.search(
        r'"ForAllValues:StringEquals"\s*=\s*\{[^}]*'
        r'"aws:TagKeys"\s*=\s*\["VectorPurpose", "ReleaseSha", "GitHubRunId"\]'
        r'[^}]*"ecs:compute-compatibility"\s*=\s*\["FARGATE"\]',
        register,
        re.DOTALL,
    )
    assert re.search(
        r'Null\s*=\s*\{[^}]*"ecs:compute-compatibility"\s*=\s*"false"',
        register,
        re.DOTALL,
    )
    assert not re.search(
        r'StringEquals\s*=\s*\{[^}]*"ecs:compute-compatibility"',
        register,
        re.DOTALL,
    )
    for statement in (run, stop):
        assert re.search(r'ArnEquals\s*=\s*\{[^}]*"ecs:cluster"', statement, re.DOTALL)
        assert not re.search(
            r'StringEquals\s*=\s*\{[^}]*"ecs:cluster"', statement, re.DOTALL
        )
    assert '"ecs:CreateAction" = [' in migrate
    assert '"RegisterTaskDefinition"' in migrate
    assert '"RunTask"' in migrate
    assert '"aws:ResourceTag/VectorPurpose" = "migration"' in migrate
    assert migrate.count("${var.name_prefix}-migration-task") == 1
    assert migrate.count("${var.name_prefix}-migration-exec") == 1
    assert "${var.name_prefix}-api-task" not in migrate
    assert "], local.secret_read_statements)" in migrate
    assert "ssm:GetParameter" in oidc
    assert "secretsmanager:GetSecretValue" in oidc
    assert "kms:Decrypt" in oidc
