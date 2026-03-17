"""Better Auth schema migration

Create auth schema, migrate user_id INT->VARCHAR(32),
drop legacy auth tables (users, refresh_tokens).

Revision ID: b1a2c3d4e5f6
Revises: a9b0c1d2e3f4
Create Date: 2026-03-17 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1a2c3d4e5f6"
down_revision: str | None = "a9b0c1d2e3f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Create auth schema for Better Auth tables
    op.execute("CREATE SCHEMA IF NOT EXISTS auth")

    # 2. Drop FK constraints referencing public.users
    op.drop_constraint(
        "user_keyword_subscriptions_user_id_fkey",
        "user_keyword_subscriptions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "watchlists_user_id_fkey",
        "watchlists",
        type_="foreignkey",
    )

    # 3. Alter user_id columns: INTEGER -> VARCHAR(32)
    op.alter_column(
        "user_keyword_subscriptions",
        "user_id",
        existing_type=sa.Integer(),
        type_=sa.String(32),
        existing_nullable=False,
        postgresql_using="user_id::varchar(32)",
    )
    op.alter_column(
        "watchlists",
        "user_id",
        existing_type=sa.Integer(),
        type_=sa.String(32),
        existing_nullable=False,
        postgresql_using="user_id::varchar(32)",
    )

    # 4. Add indexes on user_id (models define index=True)
    op.create_index(
        "ix_user_keyword_subscriptions_user_id",
        "user_keyword_subscriptions",
        ["user_id"],
    )
    op.create_index(
        "ix_watchlists_user_id",
        "watchlists",
        ["user_id"],
    )

    # 5. Drop legacy auth tables
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")


def downgrade() -> None:
    # 1. Recreate users table (with role column from a9)
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "role",
            sa.String(20),
            nullable=False,
            server_default="user",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # 2. Recreate refresh_tokens table (with revoked_at from a1b2)
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "is_revoked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])

    # 3. Drop new indexes
    op.drop_index("ix_watchlists_user_id", table_name="watchlists")
    op.drop_index(
        "ix_user_keyword_subscriptions_user_id",
        table_name="user_keyword_subscriptions",
    )

    # 4. Alter user_id back: VARCHAR(32) -> INTEGER
    # NOTE: This will fail if non-numeric user_id values exist (Better Auth nanoid)
    op.alter_column(
        "user_keyword_subscriptions",
        "user_id",
        existing_type=sa.String(32),
        type_=sa.Integer(),
        existing_nullable=False,
        postgresql_using="user_id::integer",
    )
    op.alter_column(
        "watchlists",
        "user_id",
        existing_type=sa.String(32),
        type_=sa.Integer(),
        existing_nullable=False,
        postgresql_using="user_id::integer",
    )

    # 5. Recreate FK constraints
    op.create_foreign_key(
        "user_keyword_subscriptions_user_id_fkey",
        "user_keyword_subscriptions",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "watchlists_user_id_fkey",
        "watchlists",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 6. Drop auth schema and all Better Auth tables
    op.execute("DROP SCHEMA IF EXISTS auth CASCADE")
