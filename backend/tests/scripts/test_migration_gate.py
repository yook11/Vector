"""migration_gate.py の synthetic revision 分類テスト。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from migration_gate import _pending_gate, classify, main  # noqa: E402


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


def test_contract_drop_is_manual_but_file_gate_passes(tmp_path: Path) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "contract"

def upgrade() -> None:
    op.drop_table("legacy_table")

def downgrade() -> None:
    op.create_table("legacy_table", sa.Column("id", sa.Integer(), primary_key=True))
""",
    )

    result = classify(path)

    assert result.kind == "contract"
    assert main(["--files", str(path)]) == 0


def test_missing_migration_kind_is_unknown_and_file_gate_fails(
    tmp_path: Path,
) -> None:
    path = _revision(
        tmp_path,
        """
def upgrade() -> None:
    op.add_column("articles", sa.Column("new_note", sa.String(), nullable=True))
""",
    )

    result = classify(path)

    assert result.kind == "unknown"
    assert main(["--files", str(path)]) == 1


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


def test_files_json_accepts_paths(tmp_path: Path) -> None:
    path = _revision(
        tmp_path,
        """
MIGRATION_KIND = "expand"

def upgrade() -> None:
    op.create_table("new_table", sa.Column("id", sa.Integer(), primary_key=True))
""",
    )

    assert main(["--files-json", f'["{path}"]']) == 0


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
