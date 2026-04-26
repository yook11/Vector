"""DiscoveredArticleRepository の統合テスト。

PR 2: Entity / Draft ベースの API (``save_many`` / ``find_by_url``) を追加。
``ON CONFLICT (original_url) DO NOTHING RETURNING`` による並行レース対応を検証する。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.collection.ingestion.domain import (
    ArticleCandidate,
    DiscoveredArticleDraft,
    DiscoveredArticleEntity,
)
from app.collection.ingestion.repository import DiscoveredArticleRepository
from app.models.discovered_article import DiscoveredArticle
from app.models.news_source import NewsSource
from app.shared.value_objects.safe_url import SafeUrl


def _draft(
    news_source_id: int, *, url: str, title: str = "Title"
) -> DiscoveredArticleDraft:
    """テスト用に Draft を組み立てるヘルパー。"""
    return DiscoveredArticleDraft.from_candidate(
        ArticleCandidate(url=SafeUrl(url), title=title),
        news_source_id=news_source_id,
    )


# ---------------------------------------------------------------------------
# fetch_existing_urls / add (PR 1 以前から存在する API)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_existing_urls_returns_empty_for_no_urls(
    db_session: AsyncSession,
) -> None:
    repo = DiscoveredArticleRepository(db_session)

    existing = await repo.fetch_existing_urls([])

    assert existing == set()


@pytest.mark.asyncio
async def test_fetch_existing_urls_returns_only_persisted_urls(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """DB に存在する URL のみが返り、未登録 URL は含まれない。"""
    persisted = DiscoveredArticle(
        original_title="Persisted",
        original_url="https://example.com/persisted",
        news_source_id=sample_source.id,
    )
    db_session.add(persisted)
    await db_session.commit()

    repo = DiscoveredArticleRepository(db_session)
    url_existing = SafeUrl("https://example.com/persisted")
    url_new = SafeUrl("https://example.com/new")

    existing = await repo.fetch_existing_urls([url_existing, url_new])

    assert existing == {url_existing}


@pytest.mark.asyncio
async def test_add_registers_discovered_article_in_session(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """add は session 経由で永続化対象に登録する。"""
    repo = DiscoveredArticleRepository(db_session)
    discovered = DiscoveredArticle(
        original_title="New",
        original_url=SafeUrl("https://example.com/new"),
        news_source_id=sample_source.id,
    )

    repo.add(discovered)
    await db_session.flush()

    assert discovered.id is not None


# ---------------------------------------------------------------------------
# save_many — empty / 上限制御
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_many_returns_empty_for_empty_drafts(
    db_session: AsyncSession,
) -> None:
    """empty drafts は SQL を発行せず空 list を返す (構文エラー回避)。"""
    repo = DiscoveredArticleRepository(db_session)

    result = await repo.save_many([])

    assert result == []


@pytest.mark.asyncio
async def test_save_many_raises_for_oversized_input(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """``_SAVE_MANY_LIMIT`` を超える入力は ValueError を上げる。"""
    repo = DiscoveredArticleRepository(db_session)
    drafts = [
        _draft(sample_source.id, url=f"https://example.com/{i}") for i in range(1001)
    ]

    with pytest.raises(ValueError, match="at most 1000"):
        await repo.save_many(drafts)


# ---------------------------------------------------------------------------
# save_many — 永続化と Entity 復元
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_many_persists_all_new_drafts(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """全件新規なら全 draft が Entity として返る。"""
    repo = DiscoveredArticleRepository(db_session)
    drafts = [
        _draft(sample_source.id, url="https://example.com/a", title="A"),
        _draft(sample_source.id, url="https://example.com/b", title="B"),
    ]

    entities = await repo.save_many(drafts)
    await db_session.commit()

    assert len(entities) == 2


@pytest.mark.asyncio
async def test_save_many_returns_entity_with_db_assigned_identity(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """返却 Entity は DB が採番した id と discovered_at を持つ。"""
    repo = DiscoveredArticleRepository(db_session)
    drafts = [_draft(sample_source.id, url="https://example.com/identity")]

    entities = await repo.save_many(drafts)

    assert len(entities) == 1
    entity = entities[0]
    assert isinstance(entity, DiscoveredArticleEntity)
    assert entity.id > 0
    assert entity.news_source_id == sample_source.id
    assert entity.url == SafeUrl("https://example.com/identity")
    assert entity.discovered_at.tzinfo is not None


@pytest.mark.asyncio
async def test_save_many_skips_existing_urls(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """既存 URL を含む batch は新規分のみ Entity として返る。"""
    db_session.add(
        DiscoveredArticle(
            original_title="Existing",
            original_url="https://example.com/existing",
            news_source_id=sample_source.id,
        )
    )
    await db_session.commit()

    repo = DiscoveredArticleRepository(db_session)
    drafts = [
        _draft(sample_source.id, url="https://example.com/existing"),
        _draft(sample_source.id, url="https://example.com/fresh"),
    ]

    entities = await repo.save_many(drafts)

    assert {str(e.url) for e in entities} == {"https://example.com/fresh"}


@pytest.mark.asyncio
async def test_save_many_returns_empty_when_all_urls_exist(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """全件既存なら空 list が返る。"""
    db_session.add(
        DiscoveredArticle(
            original_title="Dup",
            original_url="https://example.com/dup",
            news_source_id=sample_source.id,
        )
    )
    await db_session.commit()

    repo = DiscoveredArticleRepository(db_session)
    drafts = [_draft(sample_source.id, url="https://example.com/dup")]

    entities = await repo.save_many(drafts)

    assert entities == []


@pytest.mark.asyncio
async def test_save_many_treats_url_case_as_distinct(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """URL 大文字小文字違いは別 URL として両方 INSERT される (正規化なし)。"""
    repo = DiscoveredArticleRepository(db_session)
    drafts = [
        _draft(sample_source.id, url="https://example.com/Path"),
        _draft(sample_source.id, url="https://example.com/path"),
    ]

    entities = await repo.save_many(drafts)

    assert len(entities) == 2


@pytest.mark.asyncio
async def test_save_many_persists_safe_url_via_type_decorator(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """SafeUrl は TypeDecorator で文字列としてラウンドトリップする。"""
    repo = DiscoveredArticleRepository(db_session)
    drafts = [_draft(sample_source.id, url="https://example.com/coerce")]

    await repo.save_many(drafts)
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(DiscoveredArticle).where(
                    DiscoveredArticle.original_url
                    == SafeUrl("https://example.com/coerce")
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert isinstance(rows[0].original_url, SafeUrl)


@pytest.mark.asyncio
async def test_save_many_does_not_commit(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """save_many は INSERT 発行のみ。commit は呼び出し側の責務。"""
    repo = DiscoveredArticleRepository(db_session)
    drafts = [_draft(sample_source.id, url="https://example.com/no-commit")]

    entities = await repo.save_many(drafts)
    assert len(entities) == 1

    await db_session.rollback()
    rows = (await db_session.execute(select(DiscoveredArticle))).scalars().all()
    assert rows == []


# ---------------------------------------------------------------------------
# save_many — 並行レース統合テスト
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_many_concurrent_yields_one_winner(
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """同一 URL を並行 save_many した場合、片方のみ Entity を返す。

    ``ON CONFLICT (original_url) DO NOTHING`` の構造的並行制御を検証する。
    2 つの独立セッションで同じ URL を同時 INSERT し、合計 1 件のみ Entity が
    返ることを確認する (RETURNING の行順は保証されないため順序非依存で扱う)。
    """
    url = "https://example.com/race"

    async def _save_in_new_session() -> list[DiscoveredArticleEntity]:
        async with session_factory() as session:
            repo = DiscoveredArticleRepository(session)
            entities = await repo.save_many([_draft(sample_source.id, url=url)])
            await session.commit()
            return entities

    results = await asyncio.gather(
        _save_in_new_session(),
        _save_in_new_session(),
    )

    total = sum(len(r) for r in results)
    assert total == 1


# ---------------------------------------------------------------------------
# find_by_url
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_by_url_returns_none_for_missing_url(
    db_session: AsyncSession,
) -> None:
    """未登録 URL は None を返す。"""
    repo = DiscoveredArticleRepository(db_session)

    result = await repo.find_by_url(SafeUrl("https://example.com/missing"))

    assert result is None


@pytest.mark.asyncio
async def test_find_by_url_returns_entity_for_persisted_url(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """登録済 URL は Entity として復元される。"""
    db_session.add(
        DiscoveredArticle(
            original_title="Persisted",
            original_url="https://example.com/persisted",
            news_source_id=sample_source.id,
        )
    )
    await db_session.commit()

    repo = DiscoveredArticleRepository(db_session)
    result = await repo.find_by_url(SafeUrl("https://example.com/persisted"))

    assert isinstance(result, DiscoveredArticleEntity)
    assert result.url == SafeUrl("https://example.com/persisted")
    assert result.title == "Persisted"
    assert result.news_source_id == sample_source.id
