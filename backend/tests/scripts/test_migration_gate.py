"""migration_gate.py の synthetic revision 分類テスト。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from migration_gate import (  # noqa: E402
    MigrationGateError,
    _pending_gate,
    classify,
    decide_changed_migrations,
    main,
    pending_revision_paths,
)


def _revision(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "synthetic_revision.py"
    path.write_text(
        f"""
from alembic import op
import sqlalchemy as sa

revision = "synthetic"
down_revision = "base"

{body}
""",
        encoding="utf-8",
    )
    return path


def test_expand_add_nullable_column_is_auto_allowed(tmp_path: Path) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.add_column("articles", sa.Column("new_note", sa.String(), nullable=True))

def downgrade() -> None:
    op.drop_column("articles", "new_note")
""",
    )

    result = classify(path)

    assert result.auto_allowed is True


def test_expand_add_not_null_with_server_default_is_auto_allowed(
    tmp_path: Path,
) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.add_column(
        "articles",
        sa.Column("is_new", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

def downgrade() -> None:
    op.drop_column("articles", "is_new")
""",
    )

    result = classify(path)

    assert result.auto_allowed is True


def test_expand_add_not_null_with_none_default_is_blocked(tmp_path: Path) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.add_column(
        "articles",
        sa.Column("slug", sa.String(), nullable=False, server_default=None),
    )
""",
    )

    result = classify(path)

    assert result.auto_allowed is False
    assert any("non-null static value" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "column_constraint",
    [
        'sa.Column("author_id", sa.Integer(), sa.ForeignKey("authors.id"))',
        'sa.Column("slug", sa.String(), unique=True)',
        'sa.Column("id", sa.Integer(), primary_key=True)',
        'sa.Column("title", sa.String(), index=True)',
    ],
)
def test_expand_add_constraint_bearing_column_is_blocked(
    tmp_path: Path,
    column_constraint: str,
) -> None:
    path = _revision(
        tmp_path,
        f"""
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.add_column("articles", {column_constraint})
""",
    )

    result = classify(path)

    assert result.auto_allowed is False
    assert any(
        "constraint" in reason or "unsupported" in reason for reason in result.reasons
    )


def test_expand_drop_in_upgrade_is_mislabelled(tmp_path: Path) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.drop_column("articles", "legacy")

def downgrade() -> None:
    op.add_column("articles", sa.Column("legacy", sa.String()))
""",
    )

    result = classify(path)

    assert result.mislabelled_expand is True


def test_contract_drop_is_manual_but_file_gate_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions = tmp_path / "backend" / "alembic" / "versions"
    versions.mkdir(parents=True)
    path = _revision(
        versions,
        """
MIGRATION_KIND = "contract"

def upgrade() -> None:
    op.drop_table("legacy_table")

def downgrade() -> None:
    op.create_table("legacy_table", sa.Column("id", sa.Integer(), primary_key=True))
""",
    )

    result = classify(path)
    monkeypatch.chdir(tmp_path)

    assert result.kind == "contract"
    assert main(["--files", "backend/alembic/versions/synthetic_revision.py"]) == 0


def test_missing_migration_kind_is_unknown_and_file_gate_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions = tmp_path / "backend" / "alembic" / "versions"
    versions.mkdir(parents=True)
    path = _revision(
        versions,
        """
def upgrade() -> None:
    op.add_column("articles", sa.Column("new_note", sa.String(), nullable=True))
""",
    )

    result = classify(path)
    monkeypatch.chdir(tmp_path)

    assert result.kind == "unknown"
    assert main(["--files", "backend/alembic/versions/synthetic_revision.py"]) == 1


def test_expand_raw_update_sql_is_blocked(tmp_path: Path) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.execute("UPDATE articles SET original_title = 'x'")
""",
    )

    result = classify(path)

    assert result.auto_allowed is False


def test_expand_set_lock_timeout_sql_is_allowed(tmp_path: Path) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
""",
    )

    result = classify(path)

    assert result.auto_allowed is True


def test_expand_set_then_drop_multi_statement_sql_is_blocked(
    tmp_path: Path,
) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'; DROP TABLE articles;")
""",
    )

    result = classify(path)

    assert result.auto_allowed is False
    assert result.mislabelled_expand is True
    assert any("destructive" in reason for reason in result.reasons)


def test_expand_comment_then_drop_multi_statement_sql_is_blocked(
    tmp_path: Path,
) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.execute("COMMENT ON TABLE articles IS 'ok'; DROP TABLE articles;")
""",
    )

    result = classify(path)

    assert result.auto_allowed is False
    assert result.mislabelled_expand is True
    assert any("destructive" in reason for reason in result.reasons)


def test_expand_set_with_trailing_semicolon_is_allowed(tmp_path: Path) -> None:
    # 末尾セミコロンのみの単文は複文とみなされず allowlist を通る。
    # 本番 migration は op.execute("SET lock_timeout = '5s';") を多用するため
    # _MULTI_STATEMENT_SQL_RE を r";" に変えると正当な migration が block される。
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.execute("SET lock_timeout = '5s';")
""",
    )

    result = classify(path)

    assert result.auto_allowed is True


def test_expand_non_destructive_multi_statement_sql_is_blocked(
    tmp_path: Path,
) -> None:
    # 破壊系キーワードが無くても複文であれば allowlist から外れて block される。
    # reasons に "destructive" が含まれないことで、複文判定が destructive 検出と
    # 独立に gate を効かせている不変条件を pin する。
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.execute("SET search_path TO public; SET lock_timeout = '5s'")
""",
    )

    result = classify(path)

    assert result.auto_allowed is False
    assert not any("destructive" in reason for reason in result.reasons)


def test_expand_set_not_null_is_blocked(tmp_path: Path) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.alter_column("articles", "title", nullable=False)
""",
    )

    result = classify(path)

    assert any("nullable=False" in reason for reason in result.reasons)


def test_expand_drop_server_default_is_blocked(tmp_path: Path) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.alter_column("articles", "title", server_default=None)
""",
    )

    result = classify(path)

    assert result.auto_allowed is False
    assert any("server_default" in reason for reason in result.reasons)


def test_expand_make_nullable_is_auto_allowed(tmp_path: Path) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.alter_column(
        "articles",
        "title",
        existing_type=sa.String(),
        nullable=True,
    )
""",
    )

    result = classify(path)

    assert result.auto_allowed is True


def test_expand_not_null_add_without_default_is_blocked(tmp_path: Path) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.add_column("articles", sa.Column("slug", sa.String(), nullable=False))
""",
    )

    result = classify(path)

    assert any("without server_default" in reason for reason in result.reasons)


def test_expand_non_concurrent_index_is_blocked(tmp_path: Path) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.create_index("ix_articles_title", "articles", ["original_title"])
""",
    )

    result = classify(path)

    assert any("create_index" in reason for reason in result.reasons)


def test_expand_concurrent_index_is_allowed(tmp_path: Path) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.create_index(
        "ix_articles_title",
        "articles",
        ["original_title"],
        postgresql_concurrently=True,
    )
""",
    )

    result = classify(path)

    assert result.auto_allowed is True


@pytest.mark.parametrize(
    "options",
    [
        "unique=True, postgresql_concurrently=True",
        "unique=is_unique, postgresql_concurrently=True",
        'postgresql_where=sa.text("archived_at IS NULL"), postgresql_concurrently=True',
    ],
)
def test_expand_concurrent_unique_or_unsupported_index_is_blocked(
    tmp_path: Path,
    options: str,
) -> None:
    path = _revision(
        tmp_path,
        f"""
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.create_index(
        "ix_articles_title",
        "articles",
        ["original_title"],
        {options},
    )
""",
    )

    result = classify(path)

    assert result.auto_allowed is False
    assert any(
        "unique" in reason or "unsupported" in reason for reason in result.reasons
    )


def test_expand_concurrent_expression_index_is_blocked(tmp_path: Path) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.create_index(
        "ix_articles_title_lower",
        "articles",
        [sa.text("lower(original_title)")],
        postgresql_concurrently=True,
    )
""",
    )

    result = classify(path)

    assert result.auto_allowed is False
    assert any("expression columns" in reason for reason in result.reasons)


def test_expand_get_bind_is_blocked(tmp_path: Path) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SELECT 1"))
""",
    )

    result = classify(path)

    assert any("op.get_bind" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "operation",
    [
        'op.bulk_insert(sa.table("articles"), [{"id": 1}])',
        'op.create_primary_key("pk_articles", "articles", ["id"])',
        "op.run_async(lambda connection: None)",
    ],
)
def test_expand_non_allowlisted_alembic_operation_is_blocked(
    tmp_path: Path,
    operation: str,
) -> None:
    path = _revision(
        tmp_path,
        f"""
MIGRATION_KIND = "expand"

def upgrade() -> None:
    {operation}
""",
    )

    result = classify(path)

    assert result.auto_allowed is False
    assert result.mislabelled_expand is True
    assert any("non-allowlisted Alembic" in reason for reason in result.reasons)


def test_expand_arbitrary_helper_call_is_blocked(tmp_path: Path) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    apply_backfill()
""",
    )

    result = classify(path)

    assert result.auto_allowed is False
    assert result.mislabelled_expand is True
    assert any("helper call" in reason for reason in result.reasons)


def test_expand_non_schema_sqlalchemy_call_is_blocked(tmp_path: Path) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    sa.create_engine("postgresql://other-database")
""",
    )

    result = classify(path)

    assert result.auto_allowed is False
    assert result.mislabelled_expand is True
    assert any("helper call" in reason for reason in result.reasons)


def test_repository_expand_revisions_fit_the_explicit_allowlist() -> None:
    versions = Path(__file__).resolve().parents[2] / "alembic" / "versions"
    expand_results = [
        result
        for path in versions.glob("*.py")
        if (result := classify(path)).declared_kind == "expand"
    ]

    assert expand_results
    assert all(result.auto_allowed for result in expand_results)


def test_files_json_accepts_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions = tmp_path / "backend" / "alembic" / "versions"
    versions.mkdir(parents=True)
    path = _revision(
        versions,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.create_table("new_table", sa.Column("id", sa.Integer(), primary_key=True))
""",
    )
    monkeypatch.chdir(tmp_path)

    assert path.is_file()
    assert (
        main(
            [
                "--files-json",
                '["backend/alembic/versions/synthetic_revision.py"]',
            ]
        )
        == 0
    )


def test_files_json_rejects_workflow_command_in_filename(
    capsys: pytest.CaptureFixture[str],
) -> None:
    malicious = "backend/alembic/versions/x\n::warning::forged\n```#x.py"
    assert main(["--files-json", json.dumps([malicious])]) == 1
    output = capsys.readouterr()
    assert "::warning::forged" not in output.out
    assert "::warning::forged" not in output.err


def test_files_gate_does_not_print_invalid_declared_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    versions = tmp_path / "backend" / "alembic" / "versions"
    versions.mkdir(parents=True)
    _revision(
        versions,
        """
MIGRATION_KIND = "\\n::warning::forged\\n```\\n#"

def upgrade() -> None:
    pass
""",
    )
    monkeypatch.chdir(tmp_path)

    assert (
        main(
            [
                "--files-json",
                '["backend/alembic/versions/synthetic_revision.py"]',
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert "declared=<invalid>" in output.out
    assert "::warning::forged" not in output.out
    assert "::warning::forged" not in output.err


def test_changed_migration_decision_is_none_for_empty_range() -> None:
    assert decide_changed_migrations([]).decision == "none"


def test_changed_migration_decision_is_expand_for_expand_only(
    tmp_path: Path,
) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.add_column("articles", sa.Column("new_note", sa.String(), nullable=True))
""",
    )
    assert decide_changed_migrations([path]).decision == "expand"


def test_changed_migration_decision_is_manual_for_contract(
    tmp_path: Path,
) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "contract"

def upgrade() -> None:
    op.drop_table("legacy")
""",
    )
    assert decide_changed_migrations([path]).decision == "manual"


def test_changed_migration_decision_and_cli_reject_mixed_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions = tmp_path / "backend" / "alembic" / "versions"
    versions.mkdir(parents=True)
    expand = _revision(
        versions,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.create_table("new_table", sa.Column("id", sa.Integer(), primary_key=True))
""",
    )
    expand = expand.rename(versions / "expand_revision.py")
    contract = _revision(
        versions,
        """
MIGRATION_KIND = "contract"

def upgrade() -> None:
    op.drop_table("legacy")
""",
    )
    monkeypatch.chdir(tmp_path)
    cli_paths = [str(path.relative_to(tmp_path)) for path in (expand, contract)]
    assert (
        decide_changed_migrations([expand, contract]).decision,
        main(["--files-json", json.dumps(cli_paths)]),
    ) == ("invalid", 1)


def test_changed_migration_decision_is_invalid_for_unknown(
    tmp_path: Path,
) -> None:
    path = _revision(
        tmp_path,
        """
def upgrade() -> None:
    pass
""",
    )
    assert decide_changed_migrations([path]).decision == "invalid"


async def test_pending_gate_passes_expand_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.create_table("new_table", sa.Column("id", sa.Integer(), primary_key=True))
""",
    )

    async def fake_pending_revision_paths() -> list[Path]:
        return [path]

    monkeypatch.setattr(
        "migration_gate._pending_revision_paths",
        fake_pending_revision_paths,
    )

    assert await _pending_gate() == 0


async def test_pending_gate_blocks_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "contract"

def upgrade() -> None:
    op.drop_column("articles", "legacy")
""",
    )

    async def fake_pending_revision_paths() -> list[Path]:
        return [path]

    monkeypatch.setattr(
        "migration_gate._pending_revision_paths",
        fake_pending_revision_paths,
    )

    assert await _pending_gate() == 1


def test_unresolvable_database_revision_is_a_contract_failure() -> None:
    from alembic.script.revision import ResolutionError

    class BrokenScript:
        def get_heads(self) -> list[str]:
            return ["z16"]

        def iterate_revisions(self, _upper: str, _lower: str) -> None:
            raise ResolutionError("unknown revision", "missing")

    with pytest.raises(MigrationGateError, match="not resolvable"):
        pending_revision_paths(BrokenScript(), ["missing"])  # type: ignore[arg-type]
