"""add MDPI 4 journal news_source rows (Crossref API 経路)

Phase 3 PR 3-c-4。MDPI の Materials / Energies / Sensors / Nanomaterials
4 journal を Tier 1 ソースとして登録する。

ソース選択経緯:

- ``https://www.mdpi.com/<ISSN>/feed`` の RSS は Cloudflare WAF で
  4 ISSN 全 403 (2026-05-04 PoC 確認済)。
- OAI-PMH ``https://oai.mdpi.com/oai/oai2.php`` は 200 OK だが setSpec が
  article-type 別のみで per-journal/ISSN フィルタ不可 → 不採用。
- Crossref API ``https://api.crossref.org/works`` の per-ISSN filter 経路で
  abstract 800-2000 chars + license CC BY 4.0 + DOI 取得を確認 → 採用。

source_type は HN と同じ ``'api'``。endpoint_url には Crossref filter URL を
記録 (運用上の経路明示のため、Fetcher 側は ClassVar.ENDPOINT_URL から構築)。

attribution_label は MDPI 各 journal の CC BY 4.0 ライセンス表記。

Revision ID: o16_add_mdpi
Revises: o15_add_ornl
Create Date: 2026-05-05

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "o16_add_mdpi"
down_revision: str | None = "o15_add_ornl"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (name, source_type, endpoint_url, site_url, attribution_label)
_NEW_SOURCES = [
    (
        "MDPI Materials",
        "api",
        "https://api.crossref.org/works?filter=issn:1996-1944",
        "https://www.mdpi.com/journal/materials",
        "Materials · MDPI · CC BY 4.0",
    ),
    (
        "MDPI Energies",
        "api",
        "https://api.crossref.org/works?filter=issn:1996-1073",
        "https://www.mdpi.com/journal/energies",
        "Energies · MDPI · CC BY 4.0",
    ),
    (
        "MDPI Sensors",
        "api",
        "https://api.crossref.org/works?filter=issn:1424-8220",
        "https://www.mdpi.com/journal/sensors",
        "Sensors · MDPI · CC BY 4.0",
    ),
    (
        "MDPI Nanomaterials",
        "api",
        "https://api.crossref.org/works?filter=issn:2079-4991",
        "https://www.mdpi.com/journal/nanomaterials",
        "Nanomaterials · MDPI · CC BY 4.0",
    ),
]


def upgrade() -> None:
    sources_table = sa.table(
        "news_sources",
        sa.column("name", sa.String),
        sa.column("source_type", sa.String),
        sa.column("endpoint_url", sa.String),
        sa.column("site_url", sa.String),
        sa.column("is_active", sa.Boolean),
        sa.column("attribution_label", sa.Text),
    )
    op.bulk_insert(
        sources_table,
        [
            {
                "name": name,
                "source_type": stype,
                "endpoint_url": endpoint,
                "site_url": site,
                "is_active": True,
                "attribution_label": label,
            }
            for name, stype, endpoint, site, label in _NEW_SOURCES
        ],
    )


def downgrade() -> None:
    conn = op.get_bind()
    for name, *_ in _NEW_SOURCES:
        conn.execute(
            sa.text("DELETE FROM news_sources WHERE name = :name"),
            {"name": name},
        )
