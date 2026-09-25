"""閲覧のAPIが、vector_apiの権限で記事・カテゴリ・ブリーフィング・トレンドを返す。"""

from datetime import UTC, date, datetime

import pytest

from local_tests.api.support import (
    embedding,
    seed_article,
    seed_briefing,
    seed_trends,
    seeded_categories,
    seeded_source,
    trends_payload,
)

pytestmark = pytest.mark.asyncio


async def test_article_list_filtered_by_category(
    api_client, bff_headers, system_database
):
    """カテゴリで絞った一覧は、そのカテゴリの記事だけを返す。"""
    category, other = (await seeded_categories(system_database))[:2]
    source = await seeded_source(system_database)
    article_id = await seed_article(
        system_database,
        source=source,
        category=category,
        source_url="https://example.com/target",
        title="対象の記事",
    )
    await seed_article(
        system_database,
        source=source,
        category=other,
        source_url="https://example.com/other",
        title="別カテゴリの記事",
    )

    response = await api_client.get(
        "/api/v1/articles", params={"category": category.slug}, headers=bff_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [article_id]


async def test_article_detail_returns_requested_article(
    api_client, bff_headers, system_database
):
    """保存した記事の詳細を求めると、その記事が詳細として返る。"""
    [category, *_] = await seeded_categories(system_database)
    source = await seeded_source(system_database)
    article_id = await seed_article(
        system_database,
        source=source,
        category=category,
        source_url="https://example.com/detail",
        title="詳細の記事",
        summary="要約",
        investor_take="投資の見方",
        key_points=["要点"],
        published_at=datetime(2026, 9, 20, 9, 0, tzinfo=UTC),
        analyzed_at=datetime(2026, 9, 20, 10, 0, tzinfo=UTC),
    )

    response = await api_client.get(
        f"/api/v1/articles/{article_id}", headers=bff_headers
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": article_id,
        "translatedTitle": "詳細の記事",
        "summary": "要約",
        "investorTake": "投資の見方",
        "keyPoints": ["要点"],
        "analyzedAt": "2026-09-20T10:00:00Z",
        "category": {"slug": category.slug, "name": category.name},
        "source": {"name": source.name, "attributionLabel": source.attribution_label},
        "publishedAt": "2026-09-20T09:00:00Z",
        "original": {"title": "詳細の記事", "url": "https://example.com/detail"},
    }


async def test_similar_articles_return_five_nearest_in_order(
    api_client, bff_headers, system_database
):
    """類似記事は対象を除き、埋め込みの近い順に既定の5件を返す。"""
    [category, *_] = await seeded_categories(system_database)
    source = await seeded_source(system_database)

    async def seed(name, vector):
        return await seed_article(
            system_database,
            source=source,
            category=category,
            source_url=f"https://example.com/{name}",
            title=name,
            embedding_text=vector,
        )

    target = await seed("target", embedding(1.0, 0.0))
    # 2次元目が大きいほど対象から離れるため、この並びが近い順になる。
    candidates = [
        await seed(f"candidate-{rank}", embedding(1.0, second))
        for rank, second in enumerate((0.1, 0.3, 0.6, 1.0, 2.0, 4.0))
    ]

    response = await api_client.get(
        f"/api/v1/articles/{target}/similar", headers=bff_headers
    )

    assert response.status_code == 200
    assert [article["id"] for article in response.json()] == candidates[:5]


async def test_category_list_counts_recently_analyzed_articles(
    api_client, bff_headers, system_database
):
    """カテゴリ一覧は、直近に分析した記事の件数をカテゴリごとに返す。"""
    [category, *_] = await seeded_categories(system_database)
    await seed_article(
        system_database,
        source=await seeded_source(system_database),
        category=category,
        source_url="https://example.com/recent",
        title="直近の記事",
    )

    response = await api_client.get("/api/v1/categories", headers=bff_headers)

    assert response.status_code == 200
    counts = {item["slug"]: item["recentCount"] for item in response.json()["items"]}
    assert counts[category.slug] == 1
    assert sum(counts.values()) == 1


async def test_briefing_list_marks_categories_without_briefing(
    api_client, bff_headers, system_database
):
    """一覧は生成済みのカテゴリに最新の要約を付け、未生成のカテゴリは空にする。"""
    categories = await seeded_categories(system_database)
    [briefed, *_] = categories
    article_id = await seed_article(
        system_database,
        source=await seeded_source(system_database),
        category=briefed,
        source_url="https://example.com/briefed",
        title="取り上げた記事",
    )
    await seed_briefing(
        system_database,
        category=briefed,
        week_start=date(2026, 9, 14),
        headline="今週の見出し",
        summary="今週の要約",
        key_article_id=article_id,
        input_article_count=1,
    )

    response = await api_client.get("/api/v1/briefing", headers=bff_headers)

    assert response.status_code == 200
    # 今週の開始日と件数は実行日で変わるため、カテゴリごとの行だけを比べる。
    assert response.json()["items"] == [
        {
            "category": {"slug": category.slug, "name": category.name},
            "latest": (
                {
                    "weekStart": "2026-09-14",
                    "headline": "今週の見出し",
                    "summary": "今週の要約",
                    "inputArticleCount": 1,
                }
                if category == briefed
                else None
            ),
        }
        for category in categories
    ]


async def test_briefing_detail_returns_saved_briefing(
    api_client, bff_headers, system_database
):
    """保存したブリーフィングの詳細を求めると、取り上げた記事を含めて返る。"""
    [category, *_] = await seeded_categories(system_database)
    source = await seeded_source(system_database)
    article_id = await seed_article(
        system_database,
        source=source,
        category=category,
        source_url="https://example.com/key",
        title="取り上げた記事",
        key_points=["要点"],
        published_at=datetime(2026, 9, 20, 9, 0, tzinfo=UTC),
    )
    await seed_briefing(
        system_database,
        category=category,
        week_start=date(2026, 9, 14),
        headline="今週の見出し",
        summary="今週の要約",
        chapters=[{"heading": "資金の流れ", "body": "章の本文"}],
        key_article_id=article_id,
        significance="取り上げた理由",
        watch_points=["今後の注目点"],
        model_name="test-model",
        input_article_count=1,
        generated_at=datetime(2026, 9, 21, tzinfo=UTC),
    )

    response = await api_client.get(
        f"/api/v1/briefing/{category.slug}", headers=bff_headers
    )

    assert response.status_code == 200
    assert response.json() == {
        "state": "briefing",
        "weekStart": "2026-09-14",
        "generatedAt": "2026-09-21T00:00:00Z",
        "modelName": "test-model",
        "inputArticleCount": 1,
        "category": {"slug": category.slug, "name": category.name},
        "headline": "今週の見出し",
        "summary": "今週の要約",
        "chapters": [{"heading": "資金の流れ", "body": "章の本文"}],
        "keyArticles": [
            {
                "significance": "取り上げた理由",
                "article": {
                    "id": article_id,
                    "translatedTitle": "取り上げた記事",
                    "source": {
                        "name": source.name,
                        "attributionLabel": source.attribution_label,
                    },
                    "url": "https://example.com/key",
                    "publishedAt": "2026-09-20T09:00:00Z",
                    "keyPoints": ["要点"],
                },
            }
        ],
        "watchPoints": ["今後の注目点"],
    }


async def test_trends_returns_saved_snapshot(api_client, bff_headers, system_database):
    """トレンドは保存済みのスナップショットをそのまま返す。"""
    window_end = date(2026, 9, 20)
    payload = trends_payload(window_end)
    await seed_trends(system_database, window_end=window_end, payload=payload)

    response = await api_client.get("/api/v1/trends", headers=bff_headers)

    assert response.status_code == 200
    assert response.json() == payload
