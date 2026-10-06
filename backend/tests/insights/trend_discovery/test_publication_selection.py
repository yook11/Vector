"""公開日時に基づく集計と、記事ごとの要点選定を実DBで確認する。"""

from datetime import timedelta

import pytest
from sqlalchemy import event

from app.insights.trend_discovery.repository import TrendsRepository

from .test_repository_trends import WEEK_END, WEEK_START, WEEKS

NVIDIA = ("nvidia", "company")
OPENAI = ("openai", "company")


async def key_points(session, category, keys=(NVIDIA,)):
    return await TrendsRepository(session).get_mention_key_points(
        category_id=category.id,
        week=WEEKS.week,
        mention_keys=keys,
    )


@pytest.mark.asyncio
async def test_selects_three_latest_publications_despite_reversed_analysis_order(
    db_session, sample_categories, seed_analysis
):
    """分析順が逆でも公開日時の新しい3記事を選ぶ。"""
    category = sample_categories[0]
    for day in range(1, 5):
        await seed_analysis(
            category_id=category.id,
            published_at=WEEK_START + timedelta(days=day),
            analyzed_at=WEEK_END + timedelta(days=5 - day),
            mentions=[NVIDIA],
            content=f"published-{day}",
        )
    result = await key_points(db_session, category)
    assert result[NVIDIA] == ("published-4", "published-3", "published-2")


@pytest.mark.asyncio
async def test_uses_article_id_only_when_publication_times_tie(
    db_session, sample_categories, seed_analysis
):
    """公開日時が同じ記事はID降順で安定して選ぶ。"""
    category = sample_categories[0]
    for content in ("first", "second", "third", "fourth"):
        await seed_analysis(
            category_id=category.id,
            published_at=WEEK_START,
            analyzed_at=WEEK_END,
            mentions=[NVIDIA],
            content=content,
        )
    assert (await key_points(db_session, category))[NVIDIA] == (
        "fourth",
        "third",
        "second",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_content", [None, "", " \t\n", 123, {}, []])
async def test_invalid_content_does_not_consume_article_slot(
    db_session, sample_categories, seed_analysis, invalid_content
):
    """無効な要点だけの記事が新しくても、3記事の枠を消費しない。"""
    category = sample_categories[0]
    for day in (1, 2, 3):
        await seed_analysis(
            category_id=category.id,
            published_at=WEEK_START + timedelta(days=day),
            analyzed_at=WEEK_END,
            mentions=[NVIDIA],
            content=f"valid-{day}",
        )
    await seed_analysis(
        category_id=category.id,
        published_at=WEEK_START + timedelta(days=4),
        analyzed_at=WEEK_END,
        key_points=[(invalid_content, [NVIDIA])],
    )
    assert (await key_points(db_session, category))[NVIDIA] == (
        "valid-3",
        "valid-2",
        "valid-1",
    )


@pytest.mark.asyncio
async def test_selects_first_valid_matching_point_in_saved_order(
    db_session, sample_categories, seed_analysis
):
    """欠落本文と他メンションの要点を飛ばし、保存順で最初の有効な要点を選ぶ。"""
    category = sample_categories[0]
    article = await seed_analysis(
        category_id=category.id,
        published_at=WEEK_START,
        analyzed_at=WEEK_END,
    )
    mention = {"surface": "NVIDIA", "type": "company"}
    article.key_points = [
        {"mentions": [mention]},
        {
            "content": "unrelated",
            "mentions": [{"surface": "OpenAI", "type": "company"}],
        },
        {"content": "z-first", "mentions": [mention, mention]},
        {"content": "a-second", "mentions": [mention]},
    ]
    await db_session.flush()
    assert (await key_points(db_session, category))[NVIDIA] == ("z-first",)


@pytest.mark.asyncio
async def test_articles_without_matching_points_do_not_consume_slots(
    db_session, sample_categories, seed_analysis
):
    """要点なし・他メンションのみの記事を除外し、該当する少数の記事だけ返す。"""
    category = sample_categories[0]
    await seed_analysis(
        category_id=category.id,
        analyzed_at=WEEK_START,
        mentions=[NVIDIA],
        content="matching",
    )
    await seed_analysis(
        category_id=category.id, analyzed_at=WEEK_START + timedelta(days=1)
    )
    await seed_analysis(
        category_id=category.id,
        analyzed_at=WEEK_START + timedelta(days=2),
        mentions=[OPENAI],
        content="other",
    )
    assert await key_points(db_session, category) == {NVIDIA: ("matching",)}


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [None, {}, "invalid"])
async def test_malformed_point_arrays_are_ignored(
    db_session, sample_categories, seed_analysis, malformed
):
    """NULLと非配列の要点データは候補なしとして扱う。"""
    category = sample_categories[0]
    article = await seed_analysis(category_id=category.id, analyzed_at=WEEK_START)
    article.key_points = malformed
    await db_session.flush()
    assert await key_points(db_session, category) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [None, {}, "invalid"])
async def test_malformed_mention_arrays_are_ignored(
    db_session, sample_categories, seed_analysis, malformed
):
    """NULLと非配列のメンションは候補なしとして扱う。"""
    category = sample_categories[0]
    article = await seed_analysis(category_id=category.id, analyzed_at=WEEK_START)
    article.key_points = [{"content": "point", "mentions": malformed}]
    await db_session.flush()
    assert await key_points(db_session, category) == {}


@pytest.mark.asyncio
async def test_each_mention_receives_three_articles_regardless_of_vectors(
    db_session, sample_categories, seed_analysis
):
    """同一ベクトルや未設定の記事も、メンションごとに独立した3件へ採用する。"""
    category = sample_categories[0]
    for day, vector in ((1, [1.0]), (2, [1.0]), (3, None), (4, [1.0])):
        await seed_analysis(
            category_id=category.id,
            published_at=WEEK_START + timedelta(days=day),
            analyzed_at=WEEK_END,
            key_points=[(f"nvidia-{day}", [NVIDIA]), (f"openai-{day}", [OPENAI])],
            embedding=vector,
        )
    assert await key_points(db_session, category, (NVIDIA, OPENAI)) == {
        NVIDIA: ("nvidia-4", "nvidia-3", "nvidia-2"),
        OPENAI: ("openai-4", "openai-3", "openai-2"),
    }


@pytest.mark.asyncio
async def test_identical_text_from_distinct_articles_is_kept(
    db_session, sample_categories, seed_analysis
):
    """文章もベクトルも同じでも、別記事ならそれぞれ採用する。"""
    category = sample_categories[0]
    for _ in range(3):
        await seed_analysis(
            category_id=category.id,
            analyzed_at=WEEK_START,
            mentions=[NVIDIA],
            content="same",
            embedding=[1.0],
        )
    assert (await key_points(db_session, category))[NVIDIA] == ("same", "same", "same")


@pytest.mark.asyncio
async def test_database_returns_only_bounded_selected_content(
    db_session, sample_categories, seed_analysis
):
    """候補が多くても1クエリで識別情報と選定済み要点だけを最大6行取得する。"""
    category = sample_categories[0]
    for number in range(12):
        await seed_analysis(
            category_id=category.id,
            analyzed_at=WEEK_START,
            key_points=[
                (f"point-{number}-{index}", [NVIDIA, OPENAI]) for index in range(4)
            ],
            embedding=[1.0],
        )
    statements = []

    def capture(state):
        statements.append(state.statement)

    event.listen(db_session.sync_session, "do_orm_execute", capture)
    try:
        await key_points(db_session, category, (NVIDIA, OPENAI))
    finally:
        event.remove(db_session.sync_session, "do_orm_execute", capture)
    assert len(statements) == 1
    stmt = statements[0]
    rows = (await db_session.execute(stmt)).all()
    assert len(rows) == 6
    assert list(stmt.selected_columns.keys()) == ["match_key", "type", "content"]
    assert "embedding" not in str(stmt)


@pytest.mark.asyncio
async def test_key_points_respect_publication_window(
    db_session, sample_categories, seed_analysis
):
    """公開日時の開始境界を含み、終了境界とそれ以前の記事は除外する。"""
    category = sample_categories[0]
    for content, published_at in (
        ("before", WEEK_START - timedelta(seconds=1)),
        ("start", WEEK_START),
        ("end", WEEK_END),
    ):
        await seed_analysis(
            category_id=category.id,
            analyzed_at=WEEK_END + timedelta(days=1),
            published_at=published_at,
            mentions=[NVIDIA],
            content=content,
        )
    assert (await key_points(db_session, category))[NVIDIA] == ("start",)


@pytest.mark.asyncio
async def test_candidates_count_week_and_previous_week_by_publication(
    db_session, sample_categories, seed_analysis
):
    """分析日時によらず公開日時で週・前週・対象外を分ける。"""
    category = sample_categories[0]
    for published_at in (
        [WEEK_START] * 5
        + [WEEK_START - timedelta(days=7), WEEK_START - timedelta(seconds=1)]
        + [
            WEEK_START - timedelta(days=8),
            WEEK_END,
        ]
    ):
        await seed_analysis(
            category_id=category.id,
            analyzed_at=WEEK_END + timedelta(days=1),
            published_at=published_at,
            mentions=[NVIDIA],
        )
    result = await TrendsRepository(db_session).get_mention_candidates(
        category_id=category.id, weeks=WEEKS
    )
    assert [(item.count, item.previous_week_count) for item in result] == [(5, 2)]


@pytest.mark.asyncio
async def test_co_mentions_count_only_publications_in_the_week(
    db_session, sample_categories, seed_analysis
):
    """共起数も分析日時によらず週に公開された記事だけを数える。"""
    category = sample_categories[0]
    for published_at in (
        WEEK_START,
        WEEK_START + timedelta(days=1),
        WEEK_END,
        WEEK_START - timedelta(seconds=1),
    ):
        await seed_analysis(
            category_id=category.id,
            analyzed_at=WEEK_END + timedelta(days=1),
            published_at=published_at,
            mentions=[NVIDIA, OPENAI],
        )
    result = await TrendsRepository(db_session).get_co_mentions(
        category_id=category.id, week=WEEKS.week, mention_keys=[NVIDIA]
    )
    assert [
        (item.name.match_key, item.shared_article_count) for item in result[NVIDIA]
    ] == [("openai", 2)]


@pytest.mark.asyncio
async def test_analyzed_article_count_uses_the_week_without_requiring_points(
    db_session, sample_categories, seed_analysis
):
    """週の分析済み記事数は要点なしの記事も含め、公開日時だけで絞る。"""
    category = sample_categories[0]
    for published_at in (
        WEEK_START,
        WEEK_START + timedelta(days=1),
        WEEK_END,
        WEEK_START - timedelta(seconds=1),
    ):
        await seed_analysis(
            category_id=category.id,
            analyzed_at=WEEK_END + timedelta(days=1),
            published_at=published_at,
        )
    assert (
        await TrendsRepository(db_session).count_analyzed_articles(week=WEEKS.week) == 2
    )
