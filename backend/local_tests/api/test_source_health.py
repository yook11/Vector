"""ソースの健全性が、ソースごとに表示窓の取得結果と未完成・最終成功を示す。

画面はAPIを通して確かめ、集計の規則は観測時刻を固定してserviceを直接呼ぶ。
どちらもvector_apiの権限で読む。
"""

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

import pytest

from app.admin.source_health.repository import SourceHealthRepository
from app.admin.source_health.schemas import (
    FailureReason,
    SourceHealthItem,
    SourceHealthResponse,
)
from app.admin.source_health.service import SourceHealthService
from app.models.news_source import SourceType
from local_tests.api.support import (
    SeededSource,
    api_time,
    news_sources_by_name,
    seed_incomplete_article,
    seed_news_source,
    seed_pipeline_event,
)

pytestmark = pytest.mark.asyncio

_OBSERVED_AT = datetime(2026, 9, 20, 12, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


async def _health(session, *, window_hours: int = 24) -> SourceHealthResponse:
    service = SourceHealthService(SourceHealthRepository(session))
    return await service.get_health(window_hours=window_hours, observed_at=_OBSERVED_AT)


def _item(result: SourceHealthResponse, source: SeededSource) -> SourceHealthItem:
    return next(item for item in result.items if item.source_id == source.id)


async def _record_events(
    database,
    source_id: int | None,
    events: Iterable[tuple[str, str, str, datetime]],
) -> None:
    for stage, event_type, outcome_code, occurred_at in events:
        await seed_pipeline_event(
            database,
            stage=stage,
            event_type=event_type,
            outcome_code=outcome_code,
            occurred_at=occurred_at,
            source_id=source_id,
        )


async def _new_source(database, name: str, *, is_active: bool = True) -> SeededSource:
    slug = name.lower().replace(" ", "-")
    return await seed_news_source(
        database,
        name=name,
        endpoint_url=f"https://{slug}.example.com/feed.xml",
        is_active=is_active,
    )


async def test_source_health_summarizes_each_source(
    api_client, admin_headers, system_database
):
    """ソースの健全性は、ソースごとに表示窓の取得結果と未完成・最終成功を示す。"""
    now = datetime.now(UTC).replace(microsecond=0)
    source = await seed_news_source(
        system_database,
        name="Health Checked Source",
        endpoint_url="https://health-checked.example.com/feed.xml",
    )
    # 未完成記事を作っただけの成功と24時間より前の成功は、分析可能に数えない。
    for stage, event_type, outcome_code, occurred_at in (
        ("acquisition", "succeeded", "article_created", now - timedelta(hours=2)),
        ("completion", "succeeded", "article_completed", now - timedelta(hours=1)),
        (
            "acquisition",
            "succeeded",
            "incomplete_article_created",
            now - timedelta(hours=1),
        ),
        (
            "acquisition",
            "rejected",
            "article_conversion_rejected",
            now - timedelta(hours=1),
        ),
        ("acquisition", "failed", "fetch_timeout", now - timedelta(hours=1)),
        ("acquisition", "succeeded", "article_created", now - timedelta(days=3)),
    ):
        await seed_pipeline_event(
            system_database,
            stage=stage,
            event_type=event_type,
            outcome_code=outcome_code,
            occurred_at=occurred_at,
            source_id=source.id,
        )
    for status in ("open", "closed"):
        await seed_incomplete_article(
            system_database,
            source=source,
            url=f"https://health-checked.example.com/incomplete-{status}",
            status=status,
            created_at=now - timedelta(hours=1),
        )

    response = await api_client.get(
        "/api/v1/admin/sources/health", headers=admin_headers
    )

    assert response.status_code == 200
    checked = {
        "analyzableRate": 66.7,
        "analyzableCount": 2,
        "processedArticleCount": 3,
        "incompleteCount": 1,
        "failureReasons": [
            {"outcomeCode": "article_conversion_rejected", "count": 1},
            {"outcomeCode": "fetch_timeout", "count": 1},
        ],
        "lastSucceededAt": api_time(now - timedelta(hours=1)),
    }
    quiet = {
        "analyzableRate": None,
        "analyzableCount": 0,
        "processedArticleCount": 0,
        "incompleteCount": 0,
        "failureReasons": [],
        "lastSucceededAt": None,
    }
    assert response.json()["items"] == [
        {
            "sourceId": row["id"],
            "sourceName": row["name"],
            "sourceType": row["source_type"],
            "isActive": row["is_active"],
            **(checked if row["id"] == source.id else quiet),
        }
        for row in await news_sources_by_name(system_database)
    ]


async def test_only_created_and_completed_articles_are_analyzable(
    api_session, system_database
):
    """分析可能に数えるのは、取得のarticle_createdと補完のarticle_completedだけである。"""
    source = await _new_source(system_database, "Analyzable Source")
    await _record_events(
        system_database,
        source.id,
        [
            (
                "acquisition",
                "succeeded",
                "article_created",
                _OBSERVED_AT - timedelta(hours=1),
            ),
            (
                "acquisition",
                "succeeded",
                "article_created",
                _OBSERVED_AT - timedelta(hours=2),
            ),
            (
                "completion",
                "succeeded",
                "article_completed",
                _OBSERVED_AT - timedelta(hours=3),
            ),
            # 工程と結果が入れ違った成功と、未完成記事を作っただけの成功は数えない。
            (
                "completion",
                "succeeded",
                "article_created",
                _OBSERVED_AT - timedelta(hours=1),
            ),
            (
                "acquisition",
                "succeeded",
                "article_completed",
                _OBSERVED_AT - timedelta(hours=1),
            ),
            (
                "acquisition",
                "succeeded",
                "incomplete_article_created",
                _OBSERVED_AT - timedelta(hours=1),
            ),
        ],
    )

    result = await _health(api_session)

    assert _item(result, source) == SourceHealthItem(
        source_id=source.id,
        source_name=source.name,
        source_type=SourceType.RSS,
        is_active=True,
        analyzable_rate=100.0,
        analyzable_count=3,
        processed_article_count=3,
        incomplete_count=0,
        failure_reasons=[],
        last_succeeded_at=_OBSERVED_AT - timedelta(hours=1),
    )


async def test_processed_counts_rejected_but_not_failed(api_session, system_database):
    """処理済みは分析可能と除外（rejected）を数え、失敗（failed）は含まない。

    失敗理由は除外と失敗を合わせ、件数の多い順・同数はコード順に返す。
    """
    source = await _new_source(system_database, "Processed Source")
    hour_ago = _OBSERVED_AT - timedelta(hours=1)
    await _record_events(
        system_database,
        source.id,
        [
            ("acquisition", "succeeded", "article_created", hour_ago),
            ("acquisition", "rejected", "article_conversion_rejected", hour_ago),
            ("acquisition", "rejected", "article_conversion_rejected", hour_ago),
            ("acquisition", "failed", "fetch_timeout", hour_ago),
            ("completion", "failed", "fetch_timeout", hour_ago),
            ("acquisition", "failed", "dns_error", hour_ago),
            # 取得と補完以外の工程の失敗は数えない。
            ("curation", "failed", "ai_unavailable", hour_ago),
        ],
    )

    result = await _health(api_session)

    assert _item(result, source) == SourceHealthItem(
        source_id=source.id,
        source_name=source.name,
        source_type=SourceType.RSS,
        is_active=True,
        analyzable_rate=33.3,
        analyzable_count=1,
        processed_article_count=3,
        incomplete_count=0,
        failure_reasons=[
            FailureReason(outcome_code="article_conversion_rejected", count=2),
            FailureReason(outcome_code="fetch_timeout", count=2),
            FailureReason(outcome_code="dns_error", count=1),
        ],
        last_succeeded_at=hour_ago,
    )


@pytest.mark.parametrize(("window_hours", "analyzable_count"), [(24, 1), (168, 3)])
async def test_window_includes_its_start_and_widens_with_hours(
    api_session, system_database, window_hours, analyzable_count
):
    """表示窓は開始時刻ちょうどの記録を含み、それより前を含まない。窓を広げると古い記録も入る。"""
    source = await _new_source(system_database, "Windowed Source")
    day_ago = _OBSERVED_AT - timedelta(hours=24)
    await _record_events(
        system_database,
        source.id,
        [
            ("acquisition", "succeeded", "article_created", day_ago),
            ("acquisition", "succeeded", "article_created", day_ago - _MICROSECOND),
            (
                "acquisition",
                "succeeded",
                "article_created",
                _OBSERVED_AT - timedelta(hours=100),
            ),
        ],
    )

    result = await _health(api_session, window_hours=window_hours)

    assert _item(result, source) == SourceHealthItem(
        source_id=source.id,
        source_name=source.name,
        source_type=SourceType.RSS,
        is_active=True,
        analyzable_rate=100.0,
        analyzable_count=analyzable_count,
        processed_article_count=analyzable_count,
        incomplete_count=0,
        failure_reasons=[],
        last_succeeded_at=day_ago,
    )


async def test_incomplete_count_is_current_waiting_and_running_articles(
    api_session, system_database
):
    """未完成記事はopenとrunningを数え、表示窓より前に作られた記事も含む。"""
    source = await _new_source(system_database, "Incomplete Source")
    for status, created_at, leased_until in (
        ("open", _OBSERVED_AT - timedelta(days=3), None),
        (
            "running",
            _OBSERVED_AT - timedelta(hours=1),
            _OBSERVED_AT + timedelta(minutes=10),
        ),
        # 補完を打ち切った記事は数えない。
        ("closed", _OBSERVED_AT - timedelta(hours=1), None),
    ):
        await seed_incomplete_article(
            system_database,
            source=source,
            url=f"https://incomplete-source.example.com/{status}",
            status=status,
            created_at=created_at,
            leased_until=leased_until,
        )

    result = await _health(api_session)

    assert _item(result, source) == SourceHealthItem(
        source_id=source.id,
        source_name=source.name,
        source_type=SourceType.RSS,
        is_active=True,
        analyzable_rate=None,
        analyzable_count=0,
        processed_article_count=0,
        incomplete_count=2,
        failure_reasons=[],
        last_succeeded_at=None,
    )


async def test_last_success_is_latest_analyzable_success(api_session, system_database):
    """最後の成功は、表示窓に関係なく両工程の分析可能な成功の最新時刻とする。

    失敗と、未完成記事を作っただけの成功は含めない。
    """
    source = await _new_source(system_database, "Last Success Source")
    await _record_events(
        system_database,
        source.id,
        [
            (
                "acquisition",
                "succeeded",
                "article_created",
                _OBSERVED_AT - timedelta(days=3),
            ),
            (
                "completion",
                "succeeded",
                "article_completed",
                _OBSERVED_AT - timedelta(days=2),
            ),
            (
                "completion",
                "failed",
                "fetch_timeout",
                _OBSERVED_AT - timedelta(hours=1),
            ),
            (
                "acquisition",
                "succeeded",
                "incomplete_article_created",
                _OBSERVED_AT - timedelta(hours=1),
            ),
        ],
    )
    incomplete_only = await _new_source(system_database, "Incomplete Only Source")
    await _record_events(
        system_database,
        incomplete_only.id,
        [
            (
                "acquisition",
                "succeeded",
                "incomplete_article_created",
                _OBSERVED_AT - timedelta(hours=1),
            ),
        ],
    )

    result = await _health(api_session)

    assert [_item(result, source), _item(result, incomplete_only)] == [
        SourceHealthItem(
            source_id=source.id,
            source_name=source.name,
            source_type=SourceType.RSS,
            is_active=True,
            analyzable_rate=None,
            analyzable_count=0,
            processed_article_count=0,
            incomplete_count=0,
            failure_reasons=[FailureReason(outcome_code="fetch_timeout", count=1)],
            last_succeeded_at=_OBSERVED_AT - timedelta(days=2),
        ),
        SourceHealthItem(
            source_id=incomplete_only.id,
            source_name=incomplete_only.name,
            source_type=SourceType.RSS,
            is_active=True,
            analyzable_rate=None,
            analyzable_count=0,
            processed_article_count=0,
            incomplete_count=0,
            failure_reasons=[],
            last_succeeded_at=None,
        ),
    ]


async def test_rate_is_empty_without_processed_articles(api_session, system_database):
    """処理済みの記事が無ければ分析可能率は空になり、失敗理由は示す。"""
    source = await _new_source(system_database, "Failing Source")
    await _record_events(
        system_database,
        source.id,
        [("acquisition", "failed", "fetch_timeout", _OBSERVED_AT - timedelta(hours=1))],
    )

    result = await _health(api_session)

    assert _item(result, source) == SourceHealthItem(
        source_id=source.id,
        source_name=source.name,
        source_type=SourceType.RSS,
        is_active=True,
        analyzable_rate=None,
        analyzable_count=0,
        processed_article_count=0,
        incomplete_count=0,
        failure_reasons=[FailureReason(outcome_code="fetch_timeout", count=1)],
        last_succeeded_at=None,
    )


async def test_every_source_is_listed_by_name(api_session, system_database):
    """有効・無効・記録の無いソースを含め、全ソースを名前順に並べる。"""
    await _new_source(system_database, "Inactive Health Source", is_active=False)
    await _new_source(system_database, "Quiet Health Source")

    result = await _health(api_session)

    assert (result.window_hours, result.observed_at) == (24, _OBSERVED_AT)
    assert [
        (item.source_id, item.source_name, item.is_active) for item in result.items
    ] == [
        (row["id"], row["name"], row["is_active"])
        for row in await news_sources_by_name(system_database)
    ]


async def test_events_are_not_mixed_across_sources(api_session, system_database):
    """記録は発生したソースだけに数え、ソースを持たない記録はどのソースにも数えない。"""
    hour_ago = _OBSERVED_AT - timedelta(hours=1)
    created = await _new_source(system_database, "Created Source")
    await _record_events(
        system_database,
        created.id,
        [("acquisition", "succeeded", "article_created", hour_ago)],
    )
    rejected = await _new_source(system_database, "Rejected Source")
    await _record_events(
        system_database,
        rejected.id,
        [("acquisition", "rejected", "article_conversion_rejected", hour_ago)],
    )
    await _record_events(
        system_database,
        None,
        [
            ("acquisition", "succeeded", "article_created", hour_ago),
            ("acquisition", "rejected", "article_conversion_rejected", hour_ago),
        ],
    )

    result = await _health(api_session)

    assert [_item(result, created), _item(result, rejected)] == [
        SourceHealthItem(
            source_id=created.id,
            source_name=created.name,
            source_type=SourceType.RSS,
            is_active=True,
            analyzable_rate=100.0,
            analyzable_count=1,
            processed_article_count=1,
            incomplete_count=0,
            failure_reasons=[],
            last_succeeded_at=hour_ago,
        ),
        SourceHealthItem(
            source_id=rejected.id,
            source_name=rejected.name,
            source_type=SourceType.RSS,
            is_active=True,
            analyzable_rate=0.0,
            analyzable_count=0,
            processed_article_count=1,
            incomplete_count=0,
            failure_reasons=[
                FailureReason(outcome_code="article_conversion_rejected", count=1)
            ],
            last_succeeded_at=None,
        ),
    ]
    assert sum(item.processed_article_count for item in result.items) == 2
