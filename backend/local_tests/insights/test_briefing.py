"""週次ブリーフィングの起動と生成が、vector_insightsで投入と保存を行う。"""

from datetime import date, datetime

import pytest

from app.insights.briefing.domain.briefing import (
    BriefingChapter,
    KeyArticle,
    WatchPoint,
    WeeklyBriefingContent,
)
from app.insights.briefing.errors import BriefingLlmError
from app.queue.messages.briefing import BriefingTaskInput
from local_tests.insights.support import (
    JST,
    seed_analyzed_article,
    seed_weekly_briefing,
    seeded_categories,
    weekly_briefings,
)

pytestmark = pytest.mark.asyncio


def _content(article_id: int) -> WeeklyBriefingContent:
    return WeeklyBriefingContent(
        headline="半導体の供給が焦点になった週",
        summary="主要各社の増産計画が相次いだ。",
        chapters=[BriefingChapter(heading="増産", body="各社が増産を発表した。")],
        key_articles=[
            KeyArticle(analyzed_article_id=article_id, significance="増産の起点")
        ],
        watch_points=[WatchPoint(statement="需要が追いつくか")],
    )


async def test_weekly_dispatch_enqueues_every_category_for_last_completed_week(
    system_database, briefing_worker, enqueued_briefings
):
    """月曜の起動は直近の完了週について全カテゴリの生成を投入する。"""
    from app.queue.tasks.briefing import dispatch_weekly_briefings

    categories = await seeded_categories(system_database)

    await dispatch_weekly_briefings(ctx=briefing_worker)

    assert enqueued_briefings == [
        BriefingTaskInput(week_start=date(2026, 9, 21), category_id=category.id)
        for category in categories
    ]


async def test_generation_saves_briefing_from_articles_analyzed_in_the_week(
    system_database, briefing_worker, briefing_generator, notifier
):
    """その週に分析された記事から生成したブリーフィングを保存して通知する。"""
    from app.queue.tasks.briefing import generate_briefing_for_category

    category = (await seeded_categories(system_database))[0]
    first_id = await seed_analyzed_article(
        system_database,
        category_id=category.id,
        title="briefing-monday",
        analyzed_at=datetime(2026, 9, 21, 9, tzinfo=JST),
    )
    second_id = await seed_analyzed_article(
        system_database,
        category_id=category.id,
        title="briefing-sunday",
        analyzed_at=datetime(2026, 9, 27, 23, tzinfo=JST),
    )
    # 翌週の月曜に分析された記事は入力に含めない。
    await seed_analyzed_article(
        system_database,
        category_id=category.id,
        title="briefing-next-week",
        analyzed_at=datetime(2026, 9, 28, 0, 1, tzinfo=JST),
    )
    briefing_generator.content = _content(first_id)

    await generate_briefing_for_category(
        BriefingTaskInput(week_start=date(2026, 9, 21), category_id=category.id),
        ctx=briefing_worker,
    )

    assert briefing_generator.calls == [
        {
            "category_name": category.name,
            "week_start": date(2026, 9, 21),
            "article_ids": [first_id, second_id],
        }
    ]
    assert await weekly_briefings(system_database) == [
        {
            "week_start_date": date(2026, 9, 21),
            "category_id": category.id,
            "headline": "半導体の供給が焦点になった週",
            "summary": "主要各社の増産計画が相次いだ。",
            "chapters": [{"heading": "増産", "body": "各社が増産を発表した。"}],
            "key_articles": [
                {"analyzed_article_id": first_id, "significance": "増産の起点"}
            ],
            "watch_points": [{"statement": "需要が追いつくか"}],
            "model_name": "local-test-model",
            "input_article_count": 2,
        }
    ]
    assert notifier.tags == [(f"briefing:{category.slug}", "briefing:list")]


async def test_generation_skips_llm_for_week_without_articles(
    system_database, briefing_worker, briefing_generator, notifier
):
    """その週に分析された記事が無ければ、LLMを呼ばず保存もしない。"""
    from app.queue.tasks.briefing import generate_briefing_for_category

    category = (await seeded_categories(system_database))[0]

    await generate_briefing_for_category(
        BriefingTaskInput(week_start=date(2026, 9, 21), category_id=category.id),
        ctx=briefing_worker,
    )

    assert briefing_generator.calls == []
    assert await weekly_briefings(system_database) == []
    assert notifier.tags == []


async def test_generation_raises_llm_failure_for_retry_without_saving(
    system_database, briefing_worker, briefing_generator, notifier
):
    """LLMの呼び出しに失敗したら、保存せずに再試行のため例外を伝える。"""
    from app.queue.tasks.briefing import generate_briefing_for_category

    category = (await seeded_categories(system_database))[0]
    await seed_analyzed_article(
        system_database,
        category_id=category.id,
        title="briefing-llm-failure",
        analyzed_at=datetime(2026, 9, 23, 9, tzinfo=JST),
    )
    briefing_generator.error = BriefingLlmError(
        provider_error=RuntimeError("provider unavailable")
    )

    with pytest.raises(BriefingLlmError):
        await generate_briefing_for_category(
            BriefingTaskInput(week_start=date(2026, 9, 21), category_id=category.id),
            ctx=briefing_worker,
        )

    assert await weekly_briefings(system_database) == []
    assert notifier.tags == []


async def test_generation_keeps_briefing_already_saved_for_the_week(
    system_database, briefing_worker, briefing_generator, notifier
):
    """同じ週とカテゴリのブリーフィングが保存済みなら、LLMを呼ばずにそのまま残す。"""
    from app.queue.tasks.briefing import generate_briefing_for_category

    category = (await seeded_categories(system_database))[0]
    await seed_analyzed_article(
        system_database,
        category_id=category.id,
        title="briefing-after-save",
        analyzed_at=datetime(2026, 9, 23, 9, tzinfo=JST),
    )
    await seed_weekly_briefing(
        system_database,
        week_start=date(2026, 9, 21),
        category_id=category.id,
        headline="先に保存した見出し",
    )

    await generate_briefing_for_category(
        BriefingTaskInput(week_start=date(2026, 9, 21), category_id=category.id),
        ctx=briefing_worker,
    )

    assert briefing_generator.calls == []
    assert await weekly_briefings(system_database) == [
        {
            "week_start_date": date(2026, 9, 21),
            "category_id": category.id,
            "headline": "先に保存した見出し",
            "summary": "先の要約",
            "chapters": [{"heading": "先の章", "body": "先の本文"}],
            "key_articles": [],
            "watch_points": [{"statement": "先の論点"}],
            "model_name": "earlier-model",
            "input_article_count": 1,
        }
    ]
    assert notifier.tags == []


async def test_generation_leaves_briefing_saved_by_another_worker_during_llm_call(
    system_database, briefing_worker, briefing_generator, notifier
):
    """LLMの応答を待つ間に別workerが保存していたら、その行を残して通知しない。"""
    from app.queue.tasks.briefing import generate_briefing_for_category

    category = (await seeded_categories(system_database))[0]
    article_id = await seed_analyzed_article(
        system_database,
        category_id=category.id,
        title="briefing-race",
        analyzed_at=datetime(2026, 9, 23, 9, tzinfo=JST),
    )

    async def another_worker_saves_first():
        await seed_weekly_briefing(
            system_database,
            week_start=date(2026, 9, 21),
            category_id=category.id,
            headline="別workerが先に保存した見出し",
        )

    briefing_generator.content = _content(article_id)
    briefing_generator.before_return = another_worker_saves_first

    await generate_briefing_for_category(
        BriefingTaskInput(week_start=date(2026, 9, 21), category_id=category.id),
        ctx=briefing_worker,
    )

    assert await weekly_briefings(system_database) == [
        {
            "week_start_date": date(2026, 9, 21),
            "category_id": category.id,
            "headline": "別workerが先に保存した見出し",
            "summary": "先の要約",
            "chapters": [{"heading": "先の章", "body": "先の本文"}],
            "key_articles": [],
            "watch_points": [{"statement": "先の論点"}],
            "model_name": "earlier-model",
            "input_article_count": 1,
        }
    ]
    assert notifier.tags == []
