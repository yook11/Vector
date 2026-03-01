"""seed 72 keywords with category links

Revision ID: f52d4ecebe6b
Revises: 4bda779a1d5e
Create Date: 2026-02-28 01:29:36.068137

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'f52d4ecebe6b'
down_revision: Union[str, None] = '4bda779a1d5e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (keyword_text, category_id)
_SEED_DATA: list[tuple[str, int]] = [
    # ai_ml (id=1)
    ("large language model", 1),
    ("generative AI", 1),
    ("AI agent", 1),
    ("multimodal AI", 1),
    ("edge AI", 1),
    ("AI regulation", 1),
    ("reinforcement learning", 1),
    ("AI infrastructure", 1),
    # biotech (id=2)
    ("gene therapy", 2),
    ("CRISPR", 2),
    ("mRNA", 2),
    ("drug discovery", 2),
    ("synthetic biology", 2),
    ("precision medicine", 2),
    ("biocomputing", 2),
    # energy (id=3)
    ("nuclear fusion", 3),
    ("solid-state battery", 3),
    ("green hydrogen", 3),
    ("solar energy", 3),
    ("energy storage", 3),
    ("small modular reactor", 3),
    ("carbon capture", 3),
    # fintech (id=4)
    ("digital currency", 4),
    ("blockchain", 4),
    ("decentralized finance", 4),
    ("embedded finance", 4),
    ("regtech", 4),
    ("payment technology", 4),
    ("insurtech", 4),
    # materials (id=5)
    ("materials informatics", 5),
    ("superconductor", 5),
    ("graphene", 5),
    ("metamaterial", 5),
    ("nanomaterial", 5),
    ("biodegradable material", 5),
    ("rare earth", 5),
    # quantum (id=6)
    ("quantum computing", 6),
    ("quantum error correction", 6),
    ("quantum networking", 6),
    ("quantum sensing", 6),
    ("quantum cryptography", 6),
    ("quantum simulation", 6),
    ("quantum supremacy", 6),
    # robotics (id=7)
    ("humanoid robot", 7),
    ("autonomous vehicle", 7),
    ("industrial automation", 7),
    ("drone", 7),
    ("surgical robot", 7),
    ("swarm robotics", 7),
    ("soft robotics", 7),
    # semiconductor (id=8)
    ("EUV lithography", 8),
    ("advanced packaging", 8),
    ("AI chip", 8),
    ("RISC-V", 8),
    ("chiplet", 8),
    ("semiconductor policy", 8),
    ("photonic chip", 8),
    ("process node", 8),
    # space (id=9)
    ("satellite constellation", 9),
    ("space launch", 9),
    ("space debris", 9),
    ("lunar exploration", 9),
    ("space manufacturing", 9),
    ("earth observation", 9),
    ("space tourism", 9),
    # telecom (id=10)
    ("6G", 10),
    ("Open RAN", 10),
    ("satellite internet", 10),
    ("network slicing", 10),
    ("fiber optic", 10),
    ("edge computing", 10),
    ("spectrum allocation", 10),
]


def upgrade() -> None:
    conn = op.get_bind()

    # Insert keywords and collect auto-generated IDs
    keyword_ids: dict[str, int] = {}
    for kw_text, _ in _SEED_DATA:
        result = conn.execute(
            sa.text(
                "INSERT INTO keywords (keyword, created_at, updated_at) "
                "VALUES (:kw, NOW(), NOW()) RETURNING id"
            ),
            {"kw": kw_text},
        )
        keyword_ids[kw_text] = result.scalar_one()

    # Bulk-insert keyword_category_links
    links_table = sa.table(
        "keyword_category_links",
        sa.column("keyword_id", sa.Integer),
        sa.column("category_id", sa.Integer),
    )
    op.bulk_insert(
        links_table,
        [
            {"keyword_id": keyword_ids[kw_text], "category_id": cat_id}
            for kw_text, cat_id in _SEED_DATA
        ],
    )


def downgrade() -> None:
    conn = op.get_bind()
    # keyword_category_links rows are removed via ON DELETE CASCADE
    for kw_text, _ in _SEED_DATA:
        conn.execute(
            sa.text("DELETE FROM keywords WHERE keyword = :kw"),
            {"kw": kw_text},
        )
