"""NoiseRepository の統合テスト (PR-2 Stage 1 signal/noise フィルタ)。

振る舞い保証:
- ``exists_for_article`` の cheap 判定が article_id 単位で正しい
- ``save`` で entities が JSONB として position 順で永続化される
- ``find_by_article_id`` が JSONB を ``ExtractedEntity`` tuple に round-trip 復元する
- ``save`` の race 敗北 (UNIQUE 違反) 時は ``None`` を返す
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.domain.value_objects.entity import EntityRawType, EntitySurface
from app.analysis.extraction.domain import ExtractedEntity, ExtractionResult
from app.analysis.extraction.noise_repository import NoiseRepository
from app.models.article import Article
from app.models.news_source import NewsSource
from tests.factories.article_url import create_article_url


def _noise_result(
    title_ja: str = "ノイズタイトル",
    summary_ja: str = "ノイズ要約",
    entities: list[tuple[str, str]] | None = None,
) -> ExtractionResult:
    if entities is None:
        entities = [("Celebrity X", "person"), ("Local Event", "event")]
    return ExtractionResult(
        relevance="noise",
        title_ja=title_ja,
        summary_ja=summary_ja,
        entities=[
            ExtractedEntity(surface=EntitySurface(s), raw_type=EntityRawType(t))
            for s, t in entities
        ],
    )


async def _make_article(
    db_session: AsyncSession, sample_source: NewsSource, url: str
) -> Article:
    article_url = await create_article_url(db_session, source=sample_source, url=url)
    article = Article(
        article_url_id=article_url.id,
        source_id=sample_source.id,
        source_url=url,
        original_title="t",
        original_content="content body content body",
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    return article


# ---------------------------------------------------------------------------
# exists_for_article
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exists_for_article_returns_false_when_no_noise(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    article = await _make_article(db_session, sample_source, "https://example.com/n0")
    repo = NoiseRepository(db_session)
    assert await repo.exists_for_article(article.id) is False


@pytest.mark.asyncio
async def test_exists_for_article_returns_true_after_save(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    article = await _make_article(db_session, sample_source, "https://example.com/n1")
    repo = NoiseRepository(db_session)
    saved = await repo.save(_noise_result(), article_id=article.id, ai_model="m")
    await db_session.commit()
    assert saved is not None
    assert await repo.exists_for_article(article.id) is True


# ---------------------------------------------------------------------------
# save / round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_persists_entities_as_jsonb_in_order(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """JSONB の配列順序が AI 出力順を保持し、find で round-trip できる。"""
    article = await _make_article(db_session, sample_source, "https://example.com/n2")
    repo = NoiseRepository(db_session)
    saved = await repo.save(
        _noise_result(
            entities=[("First", "company"), ("Second", "person"), ("Third", "tech")]
        ),
        article_id=article.id,
        ai_model="gemini-test",
    )
    await db_session.commit()

    assert saved is not None
    assert tuple(e.surface.root for e in saved.entities) == ("First", "Second", "Third")

    fetched = await repo.find_by_article_id(article.id)
    assert fetched is not None
    assert tuple(e.surface.root for e in fetched.entities) == (
        "First",
        "Second",
        "Third",
    )
    assert tuple(e.raw_type.root for e in fetched.entities) == (
        "company",
        "person",
        "tech",
    )
    assert fetched.title_ja == "ノイズタイトル"
    assert fetched.ai_model == "gemini-test"


@pytest.mark.asyncio
async def test_save_accepts_empty_entities(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """entities が空でも noise 記録は永続化できる (空配列 JSONB)。"""
    article = await _make_article(db_session, sample_source, "https://example.com/n3")
    repo = NoiseRepository(db_session)
    saved = await repo.save(
        _noise_result(entities=[]),
        article_id=article.id,
        ai_model="m",
    )
    await db_session.commit()

    assert saved is not None
    assert saved.entities == ()

    fetched = await repo.find_by_article_id(article.id)
    assert fetched is not None
    assert fetched.entities == ()


@pytest.mark.asyncio
async def test_save_returns_none_on_unique_race_loss(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    """同一 article への 2 回目 save (UNIQUE 違反) は None を返す。"""
    article = await _make_article(db_session, sample_source, "https://example.com/n4")
    repo = NoiseRepository(db_session)

    first = await repo.save(_noise_result(), article_id=article.id, ai_model="m")
    await db_session.commit()
    assert first is not None

    second = await repo.save(
        _noise_result(title_ja="別タイトル"),
        article_id=article.id,
        ai_model="m",
    )
    await db_session.commit()
    assert second is None  # race 敗北は None で表現される


# ---------------------------------------------------------------------------
# find_by_article_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_by_article_id_returns_none_when_absent(
    db_session: AsyncSession, sample_source: NewsSource
) -> None:
    article = await _make_article(db_session, sample_source, "https://example.com/n5")
    repo = NoiseRepository(db_session)
    assert await repo.find_by_article_id(article.id) is None
