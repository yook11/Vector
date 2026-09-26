"""APIの試験データを所有者の接続で準備する。"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from app.insights.trend_discovery.domain.trend import (
    CategoryTrends,
    RankedMention,
    TrendsBundle,
)
from app.insights.trend_discovery.schemas import trends_from_snapshot

EMBEDDING_DIMENSIONS = 768
_TRENDS_GENERATED_AT = datetime(2026, 9, 21, tzinfo=UTC)
_BRIEFING_GENERATED_AT = datetime(2026, 9, 21, tzinfo=UTC)
_ASKED_AT = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
_ANSWERED_AT = datetime(2026, 9, 20, 9, 1, tzinfo=UTC)
# 期限回収の対象にしないrunの締切。
_FAR_DEADLINE = datetime(2100, 1, 1, tzinfo=UTC)
_SOURCE_UPDATED_AT = datetime(2026, 9, 20, tzinfo=UTC)


@dataclass(frozen=True)
class SeededCategory:
    id: int
    slug: str
    name: str


@dataclass(frozen=True)
class SeededSource:
    id: int
    name: str
    attribution_label: str | None


async def seeded_categories(database) -> list[SeededCategory]:
    async with database.connect("vector") as connection:
        rows = await connection.fetch(
            "SELECT id, slug, name FROM categories ORDER BY id"
        )
    return [SeededCategory(row["id"], row["slug"], row["name"]) for row in rows]


async def seeded_source(database) -> SeededSource:
    async with database.connect("vector") as connection:
        row = await connection.fetchrow(
            "SELECT id, name, attribution_label FROM news_sources ORDER BY id LIMIT 1"
        )
    if row is None:
        raise RuntimeError("記事の準備に必要なソースが存在しない")
    return SeededSource(row["id"], row["name"], row["attribution_label"])


def api_time(moment: datetime) -> str:
    """APIが返すUTC時刻の表記（末尾Z）にする。"""
    return moment.isoformat().replace("+00:00", "Z")


def embedding(first: float, second: float) -> str:
    """先頭2次元だけを持つ埋め込みを、halfvecのテキスト表現で返す。"""
    values = [first, second] + [0.0] * (EMBEDDING_DIMENSIONS - 2)
    return "[" + ",".join(str(value) for value in values) + "]"


async def seed_analyzable_article(
    database,
    *,
    source: SeededSource,
    source_url: str,
    title: str,
    published_at: datetime | None = None,
    created_at: datetime | None = None,
) -> int:
    """取得済みの元記事を作る。時刻を省略した場合はDBの現在時刻とする。"""
    async with database.connect("vector") as connection:
        return await connection.fetchval(
            "INSERT INTO analyzable_articles "
            "(source_id, source_url, original_title, original_content, "
            "published_at, created_at) "
            "VALUES ($1, $2, $3, 'content', COALESCE($4::timestamptz, now()), "
            "COALESCE($5::timestamptz, now())) RETURNING id",
            source.id,
            source_url,
            title,
            published_at,
            created_at,
        )


async def seed_curation(
    database, *, analyzable_article_id: int, title: str, summary: str = "summary"
) -> int:
    async with database.connect("vector") as connection:
        return await connection.fetchval(
            "INSERT INTO article_curations "
            "(analyzable_article_id, translated_title, summary) "
            "VALUES ($1, $2, $3) RETURNING id",
            analyzable_article_id,
            title,
            summary,
        )


async def seed_out_of_scope(database, *, curation_id: int) -> None:
    async with database.connect("vector") as connection:
        await connection.execute(
            "INSERT INTO out_of_scope_articles "
            "(curation_id, translated_title, summary, investor_take) "
            "VALUES ($1, 'title', 'summary', 'take')",
            curation_id,
        )


async def seed_curation_noise(database, *, analyzable_article_id: int) -> None:
    async with database.connect("vector") as connection:
        await connection.execute(
            "INSERT INTO curation_noises "
            "(analyzable_article_id, translated_title, summary) "
            "VALUES ($1, 'noise', 'noise')",
            analyzable_article_id,
        )


async def seed_assessment_exclusion(database, *, curation_id: int) -> None:
    """期限を過ぎて判定の救済から外したcurationの記録を作る。"""
    async with database.connect("vector") as connection:
        await connection.execute(
            "INSERT INTO assessment_backfill_exclusions (curation_id, reason_code) "
            "VALUES ($1, 'backfill_assessment_aged_out')",
            curation_id,
        )


async def seed_embedding_exclusion(database, *, analyzed_article_id: int) -> None:
    """期限を過ぎて埋め込みの救済から外した分析結果の記録を作る。"""
    async with database.connect("vector") as connection:
        await connection.execute(
            "INSERT INTO embedding_backfill_exclusions "
            "(analyzed_article_id, reason_code) "
            "VALUES ($1, 'backfill_embedding_aged_out')",
            analyzed_article_id,
        )


async def seed_article(
    database,
    *,
    source: SeededSource,
    category: SeededCategory,
    source_url: str,
    title: str,
    summary: str = "summary",
    investor_take: str = "take",
    key_points: Sequence[str] = (),
    embedding_text: str | None = None,
    published_at: datetime | None = None,
    analyzed_at: datetime | None = None,
    created_at: datetime | None = None,
) -> int:
    """元記事・curation・分析結果をつなげて作り、分析結果のidを返す。

    時刻を省略した場合は、DBの現在時刻で取得・公開・分析されたものとする。
    """
    article_id = await seed_analyzable_article(
        database,
        source=source,
        source_url=source_url,
        title=title,
        published_at=published_at,
        created_at=created_at,
    )
    curation_id = await seed_curation(
        database, analyzable_article_id=article_id, title=title, summary=summary
    )
    async with database.connect("vector") as connection:
        return await connection.fetchval(
            "INSERT INTO analyzed_articles "
            "(curation_id, translated_title, summary, investor_take, category_id, "
            "key_points, embedding, analyzed_at) "
            "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::text::halfvec, "
            "COALESCE($8::timestamptz, now())) RETURNING id",
            curation_id,
            title,
            summary,
            investor_take,
            category.id,
            json.dumps([{"content": point} for point in key_points]),
            embedding_text,
            analyzed_at,
        )


async def seed_briefing(
    database,
    *,
    category: SeededCategory,
    week_start: date,
    headline: str,
    summary: str,
    key_article_id: int,
    significance: str = "significance",
    chapters: Sequence[dict[str, str]] = ({"heading": "heading", "body": "body"},),
    watch_points: Sequence[str] = ("watch point",),
    model_name: str = "test-model",
    input_article_count: int = 1,
    generated_at: datetime = _BRIEFING_GENERATED_AT,
) -> None:
    async with database.connect("vector") as connection:
        await connection.execute(
            "INSERT INTO weekly_briefings "
            "(week_start_date, category_id, headline, summary, chapters, "
            "key_articles, watch_points, model_name, input_article_count, "
            "generated_at) "
            "VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8, $9, $10)",
            week_start,
            category.id,
            headline,
            summary,
            json.dumps(list(chapters)),
            json.dumps(
                [{"analyzed_article_id": key_article_id, "significance": significance}]
            ),
            json.dumps([{"statement": point} for point in watch_points]),
            model_name,
            input_article_count,
            generated_at,
        )


def trends_payload(window_end: date) -> dict:
    """製品のトレンドの型から、APIが返す形の保存内容を作る。"""
    mention = RankedMention(
        name="NVIDIA", type="company", appearance_count=30, previous_appearance_count=5
    )
    bundle = TrendsBundle(
        window_end=window_end,
        category_trends=(
            CategoryTrends(
                category_id=1,
                category_slug="ai",
                category_name="AI",
                most_mentioned=(mention,),
                fastest_growing=(mention,),
            ),
        ),
    )
    response = trends_from_snapshot(
        bundle=bundle,
        generated_at=_TRENDS_GENERATED_AT,
        source_analysis_count=42,
    )
    return response.model_dump(mode="json", by_alias=True)


async def seed_trends(database, *, window_end: date, payload: dict) -> None:
    async with database.connect("vector") as connection:
        await connection.execute(
            "INSERT INTO trends_snapshots "
            "(window_end, bundle, generated_at, source_analysis_count) "
            "VALUES ($1, $2::jsonb, $3, 42)",
            window_end,
            json.dumps(payload),
            _TRENDS_GENERATED_AT,
        )


async def watch(
    database, *, user_id: UUID, article_id: int, watched_at: datetime
) -> None:
    async with database.connect("vector") as connection:
        await connection.execute(
            "INSERT INTO watchlist_entries (user_id, analyzed_article_id, created_at) "
            "VALUES ($1, $2, $3)",
            user_id,
            article_id,
            watched_at,
        )


async def watched_article_ids(database, user_id: UUID) -> list[int]:
    async with database.connect("vector") as connection:
        rows = await connection.fetch(
            "SELECT analyzed_article_id FROM watchlist_entries "
            "WHERE user_id = $1 ORDER BY analyzed_article_id",
            user_id,
        )
    return [row["analyzed_article_id"] for row in rows]


@dataclass(frozen=True)
class SeededQuestion:
    message_id: UUID
    run_id: UUID


async def seed_thread(
    database, *, user_id: UUID, title: str, updated_at: datetime = _ASKED_AT
) -> UUID:
    async with database.connect("vector") as connection:
        return await connection.fetchval(
            "INSERT INTO agent_threads (user_id, title, updated_at) "
            "VALUES ($1, $2, $3) RETURNING id",
            user_id,
            title,
            updated_at,
        )


async def seed_question(
    database,
    *,
    thread_id: UUID,
    seq: int,
    content: str,
    status: str,
    created_at: datetime = _ASKED_AT,
    deadline_at: datetime = _FAR_DEADLINE,
    attempt_epoch: int = 0,
    error_code: str | None = None,
    quota_usage_date: date | None = None,
) -> SeededQuestion:
    """利用者の質問と、その質問に対する未回答のrunを作る。"""
    async with database.connect("vector") as connection:
        message_id = await connection.fetchval(
            "INSERT INTO agent_messages (thread_id, seq, role, content, created_at) "
            "VALUES ($1, $2, 'user', $3, $4) RETURNING id",
            thread_id,
            seq,
            content,
            created_at,
        )
        run_id = await connection.fetchval(
            "INSERT INTO agent_runs "
            "(thread_id, user_message_id, status, error_code, created_at, "
            "deadline_at, attempt_epoch, quota_usage_date) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id",
            thread_id,
            message_id,
            status,
            error_code,
            created_at,
            deadline_at,
            attempt_epoch,
            quota_usage_date,
        )
    return SeededQuestion(message_id, run_id)


async def seed_answer(
    database,
    *,
    thread_id: UUID,
    run_id: UUID,
    seq: int,
    content: str,
    created_at: datetime = _ANSWERED_AT,
) -> UUID:
    """回答メッセージを作り、そのrunを回答済みとして確定させる。"""
    async with database.connect("vector") as connection:
        async with connection.transaction():
            message_id = await connection.fetchval(
                "INSERT INTO agent_messages "
                "(thread_id, seq, role, content, created_at) "
                "VALUES ($1, $2, 'assistant', $3, $4) RETURNING id",
                thread_id,
                seq,
                content,
                created_at,
            )
            await connection.execute(
                "UPDATE agent_runs SET status = 'completed', assistant_message_id = $2 "
                "WHERE id = $1",
                run_id,
                message_id,
            )
    return message_id


async def seed_external_source(
    database,
    *,
    message_id: UUID,
    source_ref: str,
    url: str,
    title: str,
    source_name: str,
    published_at: datetime,
    evidence_claim: str,
) -> None:
    async with database.connect("vector") as connection:
        await connection.execute(
            "INSERT INTO agent_message_sources "
            "(message_id, ordinal, kind, source_ref, url, title, source_name, "
            "published_at, evidence_claim) "
            "VALUES ($1, 1, 'external_url', $2, $3, $4, $5, $6, $7)",
            message_id,
            source_ref,
            url,
            title,
            source_name,
            published_at,
            evidence_claim,
        )


async def seed_daily_quota(
    database, *, user_id: UUID, usage_date: date, used_count: int
) -> None:
    async with database.connect("vector") as connection:
        await connection.execute(
            "INSERT INTO agent_user_daily_quotas (user_id, usage_date, used_count) "
            "VALUES ($1, $2, $3)",
            user_id,
            usage_date,
            used_count,
        )


async def thread_record(database, thread_id: UUID) -> dict:
    """スレッドの持ち主・題名・メッセージ・runを、保存された順にまとめて読む。"""
    async with database.connect("vector") as connection:
        thread = await connection.fetchrow(
            "SELECT user_id, title FROM agent_threads WHERE id = $1", thread_id
        )
        messages = await connection.fetch(
            "SELECT seq, role, content FROM agent_messages "
            "WHERE thread_id = $1 ORDER BY seq",
            thread_id,
        )
        runs = await connection.fetch(
            "SELECT id, status, error_code FROM agent_runs "
            "WHERE thread_id = $1 ORDER BY created_at",
            thread_id,
        )
    return {
        "user_id": thread["user_id"],
        "title": thread["title"],
        "messages": [dict(message) for message in messages],
        "runs": [dict(run) for run in runs],
    }


async def thread_updated_at(database, thread_id: UUID) -> datetime:
    async with database.connect("vector") as connection:
        return await connection.fetchval(
            "SELECT updated_at FROM agent_threads WHERE id = $1", thread_id
        )


async def daily_quota_counts(database, user_id: UUID) -> dict[date, int]:
    async with database.connect("vector") as connection:
        rows = await connection.fetch(
            "SELECT usage_date, used_count FROM agent_user_daily_quotas "
            "WHERE user_id = $1",
            user_id,
        )
    return {row["usage_date"]: row["used_count"] for row in rows}


async def research_row_counts(database) -> dict[str, int]:
    """リサーチの履歴を持つ4表の行数を読む（ケースごとにDBは分離されている）。"""
    async with database.connect("vector") as connection:
        return {
            table: await connection.fetchval(f"SELECT count(*) FROM {table}")  # noqa: S608
            for table in (
                "agent_threads",
                "agent_messages",
                "agent_runs",
                "agent_message_sources",
            )
        }


async def seed_news_source(
    database,
    *,
    name: str,
    endpoint_url: str,
    is_active: bool = True,
    updated_at: datetime = _SOURCE_UPDATED_AT,
) -> SeededSource:
    async with database.connect("vector") as connection:
        source_id = await connection.fetchval(
            "INSERT INTO news_sources "
            "(name, source_type, site_url, endpoint_url, is_active, updated_at) "
            "VALUES ($1, 'rss', $2, $2, $3, $4) RETURNING id",
            name,
            endpoint_url,
            is_active,
            updated_at,
        )
    return SeededSource(source_id, name, None)


async def news_source_record(database, source_id: int) -> dict | None:
    """ソースの登録内容と有効状態を読む。無ければNoneを返す。"""
    async with database.connect("vector") as connection:
        row = await connection.fetchrow(
            "SELECT name, source_type, site_url, endpoint_url, is_active "
            "FROM news_sources WHERE id = $1",
            source_id,
        )
    return None if row is None else dict(row)


async def news_source_updated_at(database, source_id: int) -> datetime:
    async with database.connect("vector") as connection:
        return await connection.fetchval(
            "SELECT updated_at FROM news_sources WHERE id = $1", source_id
        )


async def news_sources_by_name(database) -> list[dict]:
    async with database.connect("vector") as connection:
        rows = await connection.fetch(
            "SELECT id, name, source_type, is_active FROM news_sources "
            "ORDER BY name, id"
        )
    return [dict(row) for row in rows]


async def seed_pipeline_event(
    database,
    *,
    stage: str,
    event_type: str,
    outcome_code: str,
    occurred_at: datetime,
    source_id: int | None = None,
) -> None:
    async with database.connect("vector") as connection:
        await connection.execute(
            "INSERT INTO pipeline_events "
            "(stage, event_type, outcome_code, source_id, occurred_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            stage,
            event_type,
            outcome_code,
            source_id,
            occurred_at,
        )


async def seed_incomplete_article(
    database,
    *,
    source: SeededSource,
    url: str,
    status: str,
    created_at: datetime,
    leased_until: datetime | None = None,
) -> None:
    """本文の補完を待つ未完成記事を作る。処理中（running）にはリースの期限を渡す。"""
    async with database.connect("vector") as connection:
        await connection.execute(
            "INSERT INTO incomplete_articles "
            "(url, source_id, source_name, status, observed_article, ready_at, "
            "leased_until, created_at) "
            "VALUES ($1, $2, $3, $4, '{}'::jsonb, $5, $6, $5)",
            url,
            source.id,
            source.name,
            status,
            created_at,
            leased_until,
        )
