"""Insightsに元記事と翻訳・要約の読み取りを許可する。"""

from sqlalchemy import text

from alembic import op

revision: str = "z30_grant_insights_publication"
down_revision: str | None = "z29_grant_agent"
branch_labels = None
depends_on = None

# GRANTは既存gateの自動許可外なので、手動確認の対象として扱う。
MIGRATION_KIND = "contract"
ROLE_NAME = "vector_insights"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    if not op.get_bind().scalar(
        text("SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = :role)"),
        {"role": ROLE_NAME},
    ):
        raise RuntimeError("Create vector_insights before migration")
    op.execute("GRANT SELECT ON public.article_curations TO vector_insights")
    op.execute("GRANT SELECT ON public.analyzable_articles TO vector_insights")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5s'")
    op.execute("REVOKE SELECT ON public.analyzable_articles FROM vector_insights")
    op.execute("REVOKE SELECT ON public.article_curations FROM vector_insights")
