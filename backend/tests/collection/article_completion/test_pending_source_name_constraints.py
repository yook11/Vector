"""``incomplete_articles`` composite FK の **動作** 保証テスト。

spec ``Pending source identity refactor.md`` #4 のみを pin する。

不変条件の所在分業:
- #1 NOT NULL / #2 composite FK / #3 (id, name) UNIQUE — ORM 定義
  (``app/models/incomplete_article.py`` / ``app/models/news_source.py``) +
  Alembic migration が SSoT。catalog 引きで重複検査しない (ORM = SSoT、
  ``[[feedback_structural_guarantee]]`` は production code 内の指針)。
- #4 — **制約が runtime で効く** ことを動作で語る (ORM 宣言だけでは PG が
  実際に拒否するかは pin できない)。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.news_source import NewsSource, SourceType
from app.shared.value_objects.safe_url import SafeUrl
from app.shared.value_objects.source_name import SourceName


@pytest.mark.asyncio
async def test_source_id_only_update_fails_with_composite_fk_violation(
    db_session: AsyncSession,
) -> None:
    """``source_id`` だけ更新して ``source_name`` を旧値のまま残す UPDATE が
    composite FK 違反で IntegrityError を上げる。

    drift シナリオ (id を別 source に向け替えるが name を旧 source のまま
    残す) が DB で構造的に遮断されることを動作で pin する。ORM 定義の
    ``ForeignKeyConstraint([source_id, source_name], [news_sources.id,
    news_sources.name])`` だけでは「PG が実際に拒否する」までは確証できない
    ため、catalog 引きではなく動作で確かめる。
    """
    # 2 source を用意 (id ≠ id、name ≠ name の独立 2 行)
    src_a = NewsSource(
        name=SourceName("Test FK Source A"),
        source_type=SourceType.RSS,
        site_url=SafeUrl("https://fk-a.example.com"),
        endpoint_url=SafeUrl("https://fk-a.example.com/feed"),
    )
    src_b = NewsSource(
        name=SourceName("Test FK Source B"),
        source_type=SourceType.RSS,
        site_url=SafeUrl("https://fk-b.example.com"),
        endpoint_url=SafeUrl("https://fk-b.example.com/feed"),
    )
    db_session.add_all([src_a, src_b])
    await db_session.flush()

    # pending を src_a (id_a, name_a) で挿入
    await db_session.execute(
        text(
            """
            INSERT INTO incomplete_articles
                (url, source_id, source_name, status, staged_attributes,
                 ready_at, attempt_count)
            VALUES (:url, :sid, :sname, 'open', '{}'::jsonb, NOW(), 0)
            """
        ),
        {
            "url": "https://example.com/fk-drift-test",
            "sid": src_a.id,
            "sname": str(src_a.name),
        },
    )

    # source_id だけ src_b の id に書き換え (source_name は src_a の name のまま)
    # → (source_id=src_b.id, source_name=src_a.name) は news_sources のどの行も
    #   指さず composite FK 違反で IntegrityError が execute() 内で raise。
    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                """
                UPDATE incomplete_articles
                SET source_id = :new_sid
                WHERE url = :url
                """
            ),
            {"new_sid": src_b.id, "url": "https://example.com/fk-drift-test"},
        )
