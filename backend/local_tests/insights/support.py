"""Insightsの試験で使う準備と観測。どちらも所有者の接続で行う。"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.insights.briefing.domain.briefing import WeeklyBriefingContent
from app.insights.briefing.domain.ready import BriefingArticle

JST = ZoneInfo("Asia/Tokyo")
EMBEDDING_DIMENSIONS = 768


@dataclass(frozen=True)
class SeededCategory:
    id: int
    slug: str
    name: str


async def seeded_categories(database) -> list[SeededCategory]:
    async with database.connect("vector") as connection:
        rows = await connection.fetch(
            "SELECT id, slug, name FROM categories ORDER BY id"
        )
    return [SeededCategory(row["id"], row["slug"], row["name"]) for row in rows]


def embedding(first: float) -> str:
    """先頭の次元だけを持つ埋め込みを、halfvecのテキスト表現で返す。"""
    values = [first, 1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 2)
    return "[" + ",".join(str(value) for value in values) + "]"


async def seed_analyzed_article(
    database,
    *,
    category_id: int,
    title: str,
    analyzed_at: datetime,
    published_at: datetime | None = None,
    key_points: list[dict] | None = None,
    embedding_text: str | None = None,
) -> int:
    """元記事・curation・分析結果をつなげて作り、分析結果のidを返す。"""
    async with database.connect("vector") as connection:
        source_id = await connection.fetchval(
            "SELECT id FROM news_sources ORDER BY id LIMIT 1"
        )
        article_id = await connection.fetchval(
            "INSERT INTO analyzable_articles "
            "(source_id, source_url, original_title, original_content, published_at) "
            "VALUES ($1, $2, $3, 'content', $4) RETURNING id",
            source_id,
            f"https://example.com/{title}",
            title,
            published_at if published_at is not None else analyzed_at,
        )
        curation_id = await connection.fetchval(
            "INSERT INTO article_curations "
            "(analyzable_article_id, translated_title, summary) "
            "VALUES ($1, $2, 'summary') RETURNING id",
            article_id,
            title,
        )
        return await connection.fetchval(
            "INSERT INTO analyzed_articles "
            "(curation_id, translated_title, summary, investor_take, category_id, "
            "key_points, embedding, analyzed_at) "
            "VALUES ($1, $2, $3, 'take', $4, $5::jsonb, $6::text::halfvec, $7) "
            "RETURNING id",
            curation_id,
            title,
            f"{title}の要約",
            category_id,
            json.dumps(key_points or []),
            embedding_text,
            analyzed_at,
        )


async def seed_trends_snapshot(
    database,
    *,
    window_end: date,
    bundle: dict,
    source_analysis_count: int,
    generated_at: datetime,
) -> None:
    async with database.connect("vector") as connection:
        await connection.execute(
            "INSERT INTO trends_snapshots "
            "(window_end, bundle, source_analysis_count, generated_at) "
            "VALUES ($1, $2::jsonb, $3, $4)",
            window_end,
            json.dumps(bundle),
            source_analysis_count,
            generated_at,
        )


async def seed_weekly_briefing(
    database, *, week_start: date, category_id: int, headline: str
) -> None:
    async with database.connect("vector") as connection:
        await connection.execute(
            "INSERT INTO weekly_briefings "
            "(week_start_date, category_id, headline, summary, chapters, "
            "key_articles, watch_points, model_name, input_article_count) "
            "VALUES ($1, $2, $3, '先の要約', "
            '\'[{"heading": "先の章", "body": "先の本文"}]\'::jsonb, '
            "'[]'::jsonb, '[{\"statement\": \"先の論点\"}]'::jsonb, "
            "'earlier-model', 1)",
            week_start,
            category_id,
            headline,
        )


async def trends_snapshots(database) -> list[dict]:
    async with database.connect("vector") as connection:
        rows = await connection.fetch(
            "SELECT window_end, bundle, source_analysis_count, generated_at "
            "FROM trends_snapshots ORDER BY window_end"
        )
    return [{**row, "bundle": json.loads(row["bundle"])} for row in rows]


async def weekly_briefings(database) -> list[dict]:
    """保存されたブリーフィングを、DBが決める採番と時刻を除いて返す。"""
    async with database.connect("vector") as connection:
        rows = await connection.fetch(
            "SELECT week_start_date, category_id, headline, summary, chapters, "
            "key_articles, watch_points, model_name, input_article_count "
            "FROM weekly_briefings ORDER BY id"
        )
    return [
        {
            **row,
            **{
                column: json.loads(row[column])
                for column in ("chapters", "key_articles", "watch_points")
            },
        }
        for row in rows
    ]


async def insights_events(database) -> list[dict]:
    """トレンドとブリーフィングの監査を記録した順に返す。payloadは値のある項目だけにする。"""
    async with database.connect("vector") as connection:
        rows = await connection.fetch(
            "SELECT stage, event_type, outcome_code, retryability, error_class, "
            "payload FROM pipeline_events "
            "WHERE stage IN ('trend_discovery', 'briefing') ORDER BY id"
        )
    return [
        {
            **row,
            "payload": {
                key: value
                for key, value in json.loads(row["payload"]).items()
                if value is not None
            },
        }
        for row in rows
    ]


@dataclass
class RecordingNotifier:
    """frontendへのキャッシュ無効化の通知を記録する。"""

    tags: list[tuple[str, ...]] = field(default_factory=list)

    async def notify(self, *, tags) -> None:
        self.tags.append(tuple(tags))


@dataclass
class BriefingGeneratorStub:
    """LLMの代わりに固定のブリーフィングを返し、受け取った入力を記録する。"""

    MODEL = "local-test-model"
    content: WeeklyBriefingContent | None = None
    error: Exception | None = None
    before_return: object = None
    calls: list[dict] = field(default_factory=list)

    async def generate(
        self,
        *,
        category_name: str,
        week_start: date,
        articles: Sequence[BriefingArticle],
    ) -> WeeklyBriefingContent:
        self.calls.append(
            {
                "category_name": category_name,
                "week_start": week_start,
                "article_ids": [article.analyzed_article_id for article in articles],
            }
        )
        if self.error is not None:
            raise self.error
        if self.before_return is not None:
            await self.before_return()
        return self.content
