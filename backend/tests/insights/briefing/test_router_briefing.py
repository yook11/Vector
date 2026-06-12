"""GET /api/v1/briefing/{categorySlug} のエンドポイントテスト。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import get_args
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import Engine, event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.insights.briefing.domain.briefing import (
    MAX_CHAPTER_BODY_LEN,
    MAX_CHAPTERS_PER_BRIEFING,
    MAX_KEY_ARTICLE_SIGNIFICANCE_LEN,
    MAX_KEY_ARTICLES_PER_BRIEFING,
    MAX_WATCH_POINT_STATEMENT_LEN,
    MAX_WATCH_POINTS_PER_BRIEFING,
)
from app.insights.briefing.domain.week import (
    latest_completed_week_start,
    now_in_jst,
)
from app.models.article import Article
from app.models.article_curation import ArticleCuration
from app.models.category import Category
from app.models.in_scope_assessment import InScopeAssessment
from app.models.news_source import NewsSource
from app.models.weekly_briefing import WeeklyBriefing

JST = ZoneInfo("Asia/Tokyo")


@pytest.fixture
async def ai_category(db_session: AsyncSession) -> Category:
    cat = Category(slug="ai", name="AI")
    db_session.add(cat)
    await db_session.commit()
    await db_session.refresh(cat)
    return cat


async def _article_id_of(db_session: AsyncSession, analysis: InScopeAssessment) -> int:
    """analysis から JSONB key_articles が参照する Article.id を SQL で引く。"""
    result = await db_session.execute(
        select(ArticleCuration.article_id).where(
            ArticleCuration.id == analysis.curation_id
        )
    )
    return result.scalar_one()


def _briefing(
    category_id: int,
    *,
    key_articles: list[dict],
    watch_points: list[dict] | None = None,
    chapters: list[dict] | None = None,
) -> WeeklyBriefing:
    # デフォルトは is None 判定で適用する (空リスト指定を silent に差し替えない)。
    return WeeklyBriefing(
        week_start_date=date(2026, 4, 20),
        category_id=category_id,
        headline="今週のヘッドライン",
        summary="今週の総括リード",
        chapters=chapters
        if chapters is not None
        else [{"heading": "資金とインフラ", "body": "今週の流れの本文"}],
        key_articles=key_articles,
        watch_points=watch_points
        if watch_points is not None
        else [{"statement": "今後どこを見るべきか"}],
        model_name="deepseek-v4-pro",
        input_article_count=1,
    )


@contextmanager
def _count_selects(sync_engine: Engine) -> Iterator[list[str]]:
    """attach 中に実行された SELECT 文を記録する (クエリ数の非比例 assert 用)。"""
    seen: list[str] = []

    def _record(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: object,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            seen.append(statement)

    event.listen(sync_engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(sync_engine, "before_cursor_execute", _record)


class TestGetBriefing:
    @pytest.mark.asyncio
    async def test_returns_404_for_unknown_category(
        self, bff_client: AsyncClient
    ) -> None:
        resp = await bff_client.get("/api/v1/briefing/nonexistent")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_requires_bff_proof(self, client: AsyncClient) -> None:
        """BFF 経由証明の無い直叩きは 401 (有効 slug でも dependency が弾く)。"""
        resp = await client.get("/api/v1/briefing/ai")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_slug",
        [
            "AI",  # 大文字混入
            "ai-ml",  # ハイフン (slug は underscore 区切り)
            "_ai",  # 先頭 underscore
            "a" * 51,  # 長過ぎ
            "%E2%80%A8",  # 異常 UTF-8 (Schemathesis Finding #3 の reproducer 系)
        ],
    )
    async def test_returns_422_for_invalid_slug_pattern(
        self, bff_client: AsyncClient, bad_slug: str
    ) -> None:
        """Path pattern 違反は 404 (DB 検索) ではなく 422 (schema reject) で弾く。"""
        resp = await bff_client.get(f"/api/v1/briefing/{bad_slug}")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_briefing(
        self, bff_client: AsyncClient, ai_category: Category
    ) -> None:
        resp = await bff_client.get("/api/v1/briefing/ai")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "empty"
        assert body["category"]["slug"] == "ai"
        # category は共有 CategoryEmbed (slug + name のみ、id は契約から撤去済)
        assert "id" not in body["category"]

    @pytest.mark.asyncio
    async def test_returns_briefing_with_embedded_article(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
        sample_source: NewsSource,
        seed_briefing_analysis,
    ) -> None:
        # decoy: assessment を持たない Article を先に 1 件入れて Article.id と
        # InScopeAssessment.id のシーケンスを意図的にずらす。テスト DB は毎回
        # RESTART IDENTITY で両 id が一致してしまい、ずらさないと下の
        # 「embed.id = 公開 id 空間」assert が判別力を持たない。
        db_session.add(
            Article(
                source_id=sample_source.id,
                source_url="https://example.com/decoy",
                original_title="decoy",
                original_content="x" * 60,
            )
        )
        await db_session.flush()

        published_at = datetime(2026, 4, 21, 9, 0, tzinfo=JST)
        analysis = await seed_briefing_analysis(
            category_id=ai_category.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
            translated_title="記事タイトル",
            published_at=published_at,
        )
        article_id = await _article_id_of(db_session, analysis)
        # decoy が効いていることの前提 assert (一致していたら下の判別が空虚になる)
        assert analysis.id != article_id
        # 新形 seed: assessment_id キー (公開 /news id 空間) で永続化
        db_session.add(
            _briefing(
                ai_category.id,
                key_articles=[
                    {"assessment_id": analysis.id, "significance": "なぜ重要か"}
                ],
            )
        )
        await db_session.commit()

        resp = await bff_client.get("/api/v1/briefing/ai")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "briefing"
        assert body["category"]["slug"] == "ai"
        assert "id" not in body["category"]
        assert body["headline"] == "今週のヘッドライン"
        assert body["summary"] == "今週の総括リード"
        assert body["chapters"] == [
            {"heading": "資金とインフラ", "body": "今週の流れの本文"}
        ]
        assert body["modelName"] == "deepseek-v4-pro"
        assert body["inputArticleCount"] == 1
        # watchPoints は wrapper のない list[str]
        assert body["watchPoints"] == ["今後どこを見るべきか"]
        # keyArticles は編集判断 + 参照記事の自己完結 nested
        assert len(body["keyArticles"]) == 1
        key_article = body["keyArticles"][0]
        assert key_article["significance"] == "なぜ重要か"
        assert "articleId" not in key_article
        article = key_article["article"]
        # embed の id は /news/{id} の公開記事 id (= analysis.id、Article.id では
        # ない)。decoy seed で両 id 空間がずれているため取り違えを判別できる。
        assert article["id"] == analysis.id
        assert article["id"] != article_id
        assert article["translatedTitle"] == "記事タイトル"
        assert article["url"].startswith("https://example.com/")
        assert article["source"]["name"] == "Test Tech Source"
        assert datetime.fromisoformat(article["publishedAt"]) == published_at
        # key_points 未抽出 (NULL) の記事は空配列に畳む
        assert article["keyPoints"] == []
        # 旧 articles[] lookup は契約から撤去済
        assert "articles" not in body

    @pytest.mark.asyncio
    async def test_article_published_at_is_null_when_unset(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
        seed_briefing_analysis,
    ) -> None:
        """``Article.published_at`` 未設定の記事は ``publishedAt: null`` で返る。"""
        analysis = await seed_briefing_analysis(
            category_id=ai_category.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
        )
        db_session.add(
            _briefing(
                ai_category.id,
                key_articles=[
                    {"assessment_id": analysis.id, "significance": "なぜ重要か"}
                ],
            )
        )
        await db_session.commit()

        resp = await bff_client.get("/api/v1/briefing/ai")
        assert resp.status_code == 200
        body = resp.json()
        assert body["keyArticles"][0]["article"]["publishedAt"] is None

    @pytest.mark.asyncio
    async def test_multiple_key_articles_pair_and_preserve_order(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
        seed_briefing_analysis,
    ) -> None:
        """各 significance が対応する記事と組になり、JSONB 記載順で返る。

        nested 化で生まれたペアリング不変条件の所有テスト。entries を作成順の
        逆順で JSONB に並べ、応答が embeds 側の順序ではなく JSONB 順を保つ
        ことも同時に判別する。
        """
        seeded = []
        for title in ["一本目", "二本目", "三本目"]:
            analysis = await seed_briefing_analysis(
                category_id=ai_category.id,
                analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
                translated_title=title,
            )
            seeded.append((analysis, title))

        # 新形 seed: assessment_id キー (公開 /news id 空間)、作成逆順で並べる
        key_articles = [
            {"assessment_id": analysis.id, "significance": f"{title}の理由"}
            for analysis, title in reversed(seeded)
        ]
        db_session.add(_briefing(ai_category.id, key_articles=key_articles))
        await db_session.commit()

        resp = await bff_client.get("/api/v1/briefing/ai")
        assert resp.status_code == 200
        body = resp.json()
        got = [
            (
                ka["significance"],
                ka["article"]["id"],
                ka["article"]["translatedTitle"],
            )
            for ka in body["keyArticles"]
        ]
        expected = [
            (f"{title}の理由", analysis.id, title)
            for analysis, title in reversed(seeded)
        ]
        assert got == expected

    @pytest.mark.asyncio
    async def test_key_points_project_content_only(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
        seed_briefing_analysis,
    ) -> None:
        """assessment の key_points JSONB から content のみ順序保持で投影する。

        mentions (trends 内部利用) は API 非公開のため response に漏らさない。
        """
        analysis = await seed_briefing_analysis(
            category_id=ai_category.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
            key_points=[
                {
                    "content": "資金調達を発表",
                    "mentions": [{"surface": "X", "type": "company"}],
                },
                {"content": "新チップを公開", "mentions": []},
            ],
        )
        db_session.add(
            _briefing(
                ai_category.id,
                key_articles=[
                    {"assessment_id": analysis.id, "significance": "なぜ重要か"}
                ],
            )
        )
        await db_session.commit()

        resp = await bff_client.get("/api/v1/briefing/ai")
        assert resp.status_code == 200
        key_points = resp.json()["keyArticles"][0]["article"]["keyPoints"]
        assert key_points == ["資金調達を発表", "新チップを公開"]

    @pytest.mark.asyncio
    async def test_attribution_label_is_passed_through(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
        sample_source: NewsSource,
        seed_briefing_analysis,
    ) -> None:
        """source に attribution_label があれば embed にそのまま載る。"""
        sample_source.attribution_label = "Tech Source 提供"
        db_session.add(sample_source)
        analysis = await seed_briefing_analysis(
            category_id=ai_category.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
        )
        db_session.add(
            _briefing(
                ai_category.id,
                key_articles=[
                    {"assessment_id": analysis.id, "significance": "なぜ重要か"}
                ],
            )
        )
        await db_session.commit()

        resp = await bff_client.get("/api/v1/briefing/ai")
        assert resp.status_code == 200
        source = resp.json()["keyArticles"][0]["article"]["source"]
        assert source["attributionLabel"] == "Tech Source 提供"

    @pytest.mark.asyncio
    async def test_attribution_label_is_null_when_unset(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
        seed_briefing_analysis,
    ) -> None:
        analysis = await seed_briefing_analysis(
            category_id=ai_category.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
        )
        db_session.add(
            _briefing(
                ai_category.id,
                key_articles=[
                    {"assessment_id": analysis.id, "significance": "なぜ重要か"}
                ],
            )
        )
        await db_session.commit()

        resp = await bff_client.get("/api/v1/briefing/ai")
        assert resp.status_code == 200
        source = resp.json()["keyArticles"][0]["article"]["source"]
        assert source["attributionLabel"] is None

    @pytest.mark.asyncio
    async def test_missing_article_raises_validation_error(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
    ) -> None:
        """参照記事の欠落は silent fallback せず ValidationError 伝播 (本番 500)。

        article non-nullable 不変条件の所有テスト。生成時 validator + 削除経路の
        不在で通常は起こりえず、起きたら failure_visibility 方針で loud に出す。
        新形 assessment_id キーで存在しない id を指定し、embed 欠落 → ValidationError
        を確認する。
        """
        db_session.add(
            _briefing(
                ai_category.id,
                key_articles=[{"assessment_id": 999_999, "significance": "なぜ重要か"}],
            )
        )
        await db_session.commit()

        with pytest.raises(ValidationError) as exc_info:
            await bff_client.get("/api/v1/briefing/ai")
        assert any(e["loc"] == ("article",) for e in exc_info.value.errors())


class TestListBriefings:
    @pytest.mark.asyncio
    async def test_returns_all_categories_with_latest_field(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
    ) -> None:
        """未生成カテゴリは latest=None で 11 行 (本テストでは 2 カテゴリ) 揃う。"""
        # 別カテゴリも追加
        other = Category(slug="robotics", name="ロボティクス")
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)

        resp = await bff_client.get("/api/v1/briefing")
        assert resp.status_code == 200
        body = resp.json()
        assert "currentWeekStart" in body
        slugs = [item["category"]["slug"] for item in body["items"]]
        assert "ai" in slugs and "robotics" in slugs
        # どちらも未生成なので latest は None
        for item in body["items"]:
            assert item["latest"] is None
        # 生成済が無いので解析記事総数は 0
        assert body["totalArticles"] == 0

    @pytest.mark.asyncio
    async def test_includes_headline_for_generated_item(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
        seed_briefing_analysis,
    ) -> None:
        # 契約1で一覧も embed fetch するため、実 assessment_id が必要。
        analysis = await seed_briefing_analysis(
            category_id=ai_category.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
        )
        db_session.add(
            _briefing(
                ai_category.id,
                key_articles=[
                    {"assessment_id": analysis.id, "significance": "なぜ重要か"}
                ],
            )
        )
        await db_session.commit()

        resp = await bff_client.get("/api/v1/briefing")
        assert resp.status_code == 200
        body = resp.json()
        ai_item = next(i for i in body["items"] if i["category"]["slug"] == "ai")
        assert ai_item["latest"] is not None
        assert ai_item["latest"]["weekStart"] == "2026-04-20"
        # 一覧は短い headline をそのまま返す (旧 headlineExcerpt 抜粋ロジックは廃止)
        assert ai_item["latest"]["headline"] == "今週のヘッドライン"
        # バンドカード用に summary / 件数も同梱する
        assert ai_item["latest"]["summary"] == "今週の総括リード"
        assert ai_item["latest"]["inputArticleCount"] == 1

    @pytest.mark.asyncio
    async def test_total_articles_counts_only_current_week(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
        seed_briefing_analysis,
    ) -> None:
        """totalArticles は今週生成された briefing のみ合計し、古い週の stale
        briefing (生成が遅れたカテゴリの latest) は含めない。"""
        current_week = latest_completed_week_start(now_in_jst())
        old_week = current_week - timedelta(days=7)

        robotics = Category(slug="robotics", name="ロボティクス")
        db_session.add(robotics)
        await db_session.commit()
        await db_session.refresh(robotics)

        # 契約1で一覧も embed fetch するため、カテゴリごとに実 assessment_id が必要。
        ai_analysis = await seed_briefing_analysis(
            category_id=ai_category.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
        )
        robotics_analysis = await seed_briefing_analysis(
            category_id=robotics.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
        )

        # ai は今週分 (count=7)、robotics は古い週の stale briefing (count=40)
        seeds = {
            ai_category.id: (current_week, 7, ai_analysis.id),
            robotics.id: (old_week, 40, robotics_analysis.id),
        }
        for category_id, (week, count, assessment_id) in seeds.items():
            db_session.add(
                WeeklyBriefing(
                    week_start_date=week,
                    category_id=category_id,
                    headline="h",
                    summary="s",
                    chapters=[{"heading": "h", "body": "b"}],
                    key_articles=[
                        {"assessment_id": assessment_id, "significance": "s"}
                    ],
                    watch_points=[{"statement": "w"}],
                    model_name="deepseek-v4-pro",
                    input_article_count=count,
                )
            )
        await db_session.commit()

        resp = await bff_client.get("/api/v1/briefing")
        assert resp.status_code == 200
        body = resp.json()
        # 今週分の 7 のみ。古い週の 40 は「今週の解析量」に含めない
        assert body["totalArticles"] == 7

    @pytest.mark.asyncio
    async def test_orders_items_by_category_id(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
    ) -> None:
        """並びは Category.id 昇順 (= 登録順)。契約に id は無いので slug 列で見る。"""
        # ai (先に登録 = id 小) → bio の順で返る想定
        b_cat = Category(slug="bio", name="バイオ")
        db_session.add(b_cat)
        await db_session.commit()
        await db_session.refresh(b_cat)

        resp = await bff_client.get("/api/v1/briefing")
        body = resp.json()
        slugs = [item["category"]["slug"] for item in body["items"]]
        assert slugs == ["ai", "bio"]

    @pytest.mark.asyncio
    async def test_list_includes_key_articles_for_generated_item(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
        sample_source: NewsSource,
        seed_briefing_analysis,
    ) -> None:
        """一覧の latest.keyArticles[].article は ArticleBrief (一覧カード契約) で入る。

        article.id は /news/{id} と同じ公開 id 空間 (= InScopeAssessment.id)。
        decoy Article でシーケンスをずらして id 空間の判別力を担保する。
        """
        # decoy: Article.id と InScopeAssessment.id のシーケンスを意図的にずらす。
        db_session.add(
            Article(
                source_id=sample_source.id,
                source_url="https://example.com/decoy-list",
                original_title="decoy",
                original_content="x" * 60,
            )
        )
        await db_session.flush()

        published_at = datetime(2026, 4, 21, 9, 0, tzinfo=JST)
        analysis = await seed_briefing_analysis(
            category_id=ai_category.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
            translated_title="一覧記事タイトル",
            published_at=published_at,
            key_points=[
                {"content": "資金調達を発表", "mentions": []},
                {"content": "新チップを公開", "mentions": []},
            ],
        )
        article_id = await _article_id_of(db_session, analysis)
        assert analysis.id != article_id  # decoy が効いていることの前提 assert
        db_session.add(
            _briefing(
                ai_category.id,
                key_articles=[
                    {"assessment_id": analysis.id, "significance": "一覧での重要理由"}
                ],
            )
        )
        await db_session.commit()

        resp = await bff_client.get("/api/v1/briefing")
        assert resp.status_code == 200
        body = resp.json()
        ai_item = next(i for i in body["items"] if i["category"]["slug"] == "ai")
        assert ai_item["latest"] is not None
        key_articles = ai_item["latest"]["keyArticles"]
        assert len(key_articles) == 1
        ka = key_articles[0]
        assert ka["significance"] == "一覧での重要理由"
        assert "articleId" not in ka
        article = ka["article"]
        # article.id は公開 id 空間 (= analysis.id)。decoy で Article.id とずれている。
        assert article["id"] == analysis.id
        assert article["id"] != article_id
        assert article["translatedTitle"] == "一覧記事タイトル"
        assert article["category"]["slug"] == "ai"
        assert article["source"]["name"] == "Test Tech Source"
        assert datetime.fromisoformat(article["publishedAt"]) == published_at
        # build_brief 経由: content のみ投影 + keyPoints 非空なら summaryPreview null
        assert article["keyPoints"] == ["資金調達を発表", "新チップを公開"]
        assert article["summaryPreview"] is None
        # ArticleBrief は url を持たない (原文リンクは記事詳細の担当)
        assert "url" not in article

    @pytest.mark.asyncio
    async def test_list_includes_watch_points_for_generated_item(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
        seed_briefing_analysis,
    ) -> None:
        """一覧の latest に watchPoints が string[] で入る。

        JSONB の [{'statement': ...}] wrapper を flatten して list[str] を返す。
        """
        analysis = await seed_briefing_analysis(
            category_id=ai_category.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
        )
        db_session.add(
            _briefing(
                ai_category.id,
                key_articles=[{"assessment_id": analysis.id, "significance": "s"}],
                watch_points=[
                    {"statement": "注目点A"},
                    {"statement": "注目点B"},
                ],
            )
        )
        await db_session.commit()

        resp = await bff_client.get("/api/v1/briefing")
        assert resp.status_code == 200
        body = resp.json()
        ai_item = next(i for i in body["items"] if i["category"]["slug"] == "ai")
        assert ai_item["latest"] is not None
        assert ai_item["latest"]["watchPoints"] == ["注目点A", "注目点B"]

    @pytest.mark.asyncio
    async def test_list_missing_article_raises_validation_error(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
    ) -> None:
        """一覧で参照記事が欠落しても silent fallback しない (failure visibility)。

        article non-nullable 不変条件の一覧経路所有テスト。
        詳細経路の test_missing_article_raises_validation_error と対称。
        """
        db_session.add(
            _briefing(
                ai_category.id,
                key_articles=[{"assessment_id": 999_999, "significance": "s"}],
            )
        )
        await db_session.commit()

        with pytest.raises(ValidationError) as exc_info:
            await bff_client.get("/api/v1/briefing")
        # loc 末尾が 'article' であることだけ見て、ネスト深さの実装スタイルを問わない。
        assert any(e["loc"][-1] == "article" for e in exc_info.value.errors())

    @pytest.mark.asyncio
    async def test_list_key_articles_distributed_per_category(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
        seed_briefing_analysis,
    ) -> None:
        """全カテゴリの assessment_id を set マージして embed を1回 fetch した後、
        各 latest への分配が正しく行われる。

        cross-category 汚染(マップを全 summary に配る等)があると各カテゴリの
        keyArticles に他カテゴリの記事 id が混入するため id レベルで判別する。
        latest=null カテゴリは分配後も null のままであることも確認する。
        """
        # robotics カテゴリ(latest あり) と bio カテゴリ(latest なし) を追加する。
        robotics = Category(slug="robotics", name="ロボティクス")
        bio = Category(slug="bio", name="バイオ")
        db_session.add(robotics)
        db_session.add(bio)
        await db_session.commit()
        await db_session.refresh(robotics)
        await db_session.refresh(bio)

        ai_analysis = await seed_briefing_analysis(
            category_id=ai_category.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
            translated_title="AI カテゴリ記事",
        )
        robotics_analysis = await seed_briefing_analysis(
            category_id=robotics.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
            translated_title="ロボティクス記事",
        )
        # id 空間が別物であることを前提 assert する。
        assert ai_analysis.id != robotics_analysis.id

        db_session.add(
            _briefing(
                ai_category.id,
                key_articles=[
                    {"assessment_id": ai_analysis.id, "significance": "AI の重要理由"}
                ],
            )
        )
        db_session.add(
            _briefing(
                robotics.id,
                key_articles=[
                    {
                        "assessment_id": robotics_analysis.id,
                        "significance": "ロボティクスの重要理由",
                    }
                ],
            )
        )
        # bio は briefing 未生成のまま (latest=null)。
        await db_session.commit()

        resp = await bff_client.get("/api/v1/briefing")
        assert resp.status_code == 200
        body = resp.json()

        items_by_slug = {item["category"]["slug"]: item for item in body["items"]}

        # ai: 自カテゴリの analysis.id のみ持つ。
        ai_latest = items_by_slug["ai"]["latest"]
        assert ai_latest is not None
        ai_ids = [ka["article"]["id"] for ka in ai_latest["keyArticles"]]
        assert ai_ids == [ai_analysis.id]
        # 他カテゴリの article が混入していないことを id レベルで確認する。
        assert robotics_analysis.id not in ai_ids
        # source.attributionLabel は未設定ソースで null になる。
        assert (
            ai_latest["keyArticles"][0]["article"]["source"]["attributionLabel"] is None
        )

        # robotics: 自カテゴリの analysis.id のみ持つ。
        robotics_latest = items_by_slug["robotics"]["latest"]
        assert robotics_latest is not None
        robotics_ids = [ka["article"]["id"] for ka in robotics_latest["keyArticles"]]
        assert robotics_ids == [robotics_analysis.id]
        assert ai_analysis.id not in robotics_ids

        # bio: latest=null のまま。分配で None が別値に書き換わらないことを確認する。
        assert items_by_slug["bio"]["latest"] is None

    @pytest.mark.asyncio
    async def test_list_query_count_independent_of_category_count(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
        seed_briefing_analysis,
    ) -> None:
        """一覧の SELECT 数が生成済みカテゴリ数に比例しない (per-category N+1 の検出)。

        現実装 (embed fetch なし) でも green で生まれる前方ガード。契約1の
        バッチ組立 (set マージ → 1回 fetch) が per-category fetch に退行すると
        1 → 3 カテゴリの差分で落ちる。
        """
        analysis = await seed_briefing_analysis(
            category_id=ai_category.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
        )
        db_session.add(
            _briefing(
                ai_category.id,
                key_articles=[{"assessment_id": analysis.id, "significance": "s"}],
            )
        )
        await db_session.commit()

        sync_engine = db_session.get_bind()
        assert isinstance(sync_engine, Engine)
        with _count_selects(sync_engine) as with_one_category:
            resp = await bff_client.get("/api/v1/briefing")
        assert resp.status_code == 200

        for slug, name in [("robotics", "ロボティクス"), ("bio", "バイオ")]:
            cat = Category(slug=slug, name=name)
            db_session.add(cat)
            await db_session.commit()
            await db_session.refresh(cat)
            extra = await seed_briefing_analysis(
                category_id=cat.id,
                analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
            )
            db_session.add(
                _briefing(
                    cat.id,
                    key_articles=[{"assessment_id": extra.id, "significance": "s"}],
                )
            )
            await db_session.commit()

        with _count_selects(sync_engine) as with_three_categories:
            resp = await bff_client.get("/api/v1/briefing")
        assert resp.status_code == 200

        assert len(with_three_categories) == len(with_one_category)

    @pytest.mark.asyncio
    async def test_list_returns_empty_arrays_without_degrade(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
    ) -> None:
        """key_articles=[] / watch_points=[] の行は null / 欠落でなく [] のまま返る。

        生成経路は domain の min_length=1 で空を作らないため DB 直書き由来の
        異常データシナリオ。response 層は隠さず空配列のまま可視化し、
        空 briefing が embed のバッチ組立に何も寄与しないエッジも同時に固定する。
        """
        db_session.add(_briefing(ai_category.id, key_articles=[], watch_points=[]))
        await db_session.commit()

        resp = await bff_client.get("/api/v1/briefing")
        assert resp.status_code == 200
        ai_item = next(i for i in resp.json()["items"] if i["category"]["slug"] == "ai")
        assert ai_item["latest"] is not None
        assert ai_item["latest"]["keyArticles"] == []
        assert ai_item["latest"]["watchPoints"] == []


class TestBriefingResponseSizeGuard:
    """red-team F10: 共有 read 経路で巨大 briefing JSONB が response として
    流れる経路を構造的に塞ぐ。

    AUTH-N4 / AUTH-C1 経由で attacker が DB に巨大 key_articles / watch_points を
    直書きしたシナリオ。key_articles の件数は router の count guard が embed
    fetch 前に弾き、それ以外は Field(max_length=...) が `_BriefingDetailKeyArticle` /
    `_BriefingArticleEmbed` / `BriefingDetail(...)` 構築時に発火して、response に
    巨大 JSONB が含まれることを構造的に防ぐ。

    per-item ガード (significance 等) は embed 組立中に発火するため、該当ケースは
    実記事を seed し、ValidationError の errors() type を assert して
    「missing article で落ちた偽の緑」を排除する。
    """

    def _persist(
        self,
        ai_category: Category,
        *,
        key_articles: list[dict] | None = None,
        watch_points: list[dict] | None = None,
        chapters: list[dict] | None = None,
    ) -> WeeklyBriefing:
        # デフォルトは is None 判定で適用する (空リスト指定を silent に差し替えない)。
        return WeeklyBriefing(
            week_start_date=date(2026, 4, 20),
            category_id=ai_category.id,
            headline="h",
            summary="s",
            chapters=chapters
            if chapters is not None
            else [{"heading": "h", "body": "b"}],
            key_articles=key_articles if key_articles is not None else [],
            watch_points=watch_points
            if watch_points is not None
            else [{"statement": "w"}],
            model_name="deepseek-v4-pro",
            input_article_count=1,
        )

    @pytest.mark.asyncio
    async def test_oversize_key_articles_rejected_before_embed_fetch(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
    ) -> None:
        """key_articles 数が上限超なら embed fetch より先に too_long で落ちる。

        実記事を一切 seed しないのが判別の要: 件数ガードが fetch / 組立より
        後に動く実装なら missing-article (loc=article) の ValidationError に
        変わるため、too_long の assert が fail-fast 順序の所有 assert になる。
        """
        oversized = [
            {"assessment_id": i + 1, "significance": f"s{i}"}
            for i in range(MAX_KEY_ARTICLES_PER_BRIEFING + 1)
        ]
        db_session.add(self._persist(ai_category, key_articles=oversized))
        await db_session.commit()

        with pytest.raises(ValidationError) as exc_info:
            await bff_client.get("/api/v1/briefing/ai")
        assert any(e["type"] == "too_long" for e in exc_info.value.errors())

    @pytest.mark.asyncio
    async def test_oversize_significance_rejected_by_response_model(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
        seed_briefing_analysis,
    ) -> None:
        """1 件の significance が上限超なら共有 read で ValidationError 伝播。"""
        analysis = await seed_briefing_analysis(
            category_id=ai_category.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
        )
        db_session.add(
            self._persist(
                ai_category,
                key_articles=[
                    {
                        "assessment_id": analysis.id,
                        "significance": "x" * (MAX_KEY_ARTICLE_SIGNIFICANCE_LEN + 1),
                    }
                ],
            )
        )
        await db_session.commit()

        with pytest.raises(ValidationError) as exc_info:
            await bff_client.get("/api/v1/briefing/ai")
        assert any(e["type"] == "string_too_long" for e in exc_info.value.errors())

    @pytest.mark.asyncio
    async def test_oversize_key_point_content_rejected_by_response_model(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
        seed_briefing_analysis,
    ) -> None:
        """keyPoint 1 件が上限超なら embed 構築時に ValidationError 伝播。

        上限 500 は assessment 側入口契約 (domain/result.py) と同値の F10 ガード。
        """
        analysis = await seed_briefing_analysis(
            category_id=ai_category.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
            key_points=[{"content": "x" * 501, "mentions": []}],
        )
        db_session.add(
            self._persist(
                ai_category,
                key_articles=[{"assessment_id": analysis.id, "significance": "s"}],
            )
        )
        await db_session.commit()

        with pytest.raises(ValidationError) as exc_info:
            await bff_client.get("/api/v1/briefing/ai")
        assert any(e["type"] == "string_too_long" for e in exc_info.value.errors())

    @pytest.mark.asyncio
    async def test_oversize_key_points_count_rejected_by_response_model(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
        seed_briefing_analysis,
    ) -> None:
        """keyPoints 件数が上限超なら embed 構築時に ValidationError 伝播。

        上限 10 件は assessment 側入口契約 (domain/result.py) と同値の F10 ガード。
        """
        analysis = await seed_briefing_analysis(
            category_id=ai_category.id,
            analyzed_at=datetime(2026, 4, 22, 12, 0, tzinfo=JST),
            key_points=[{"content": f"p{i}", "mentions": []} for i in range(11)],
        )
        db_session.add(
            self._persist(
                ai_category,
                key_articles=[{"assessment_id": analysis.id, "significance": "s"}],
            )
        )
        await db_session.commit()

        with pytest.raises(ValidationError) as exc_info:
            await bff_client.get("/api/v1/briefing/ai")
        assert any(e["type"] == "too_long" for e in exc_info.value.errors())

    @pytest.mark.asyncio
    async def test_oversize_watch_points_rejected_by_response_model(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
    ) -> None:
        """watch_points 数が上限超なら共有 read で ValidationError 伝播。"""
        oversized = [
            {"statement": f"w{i}"} for i in range(MAX_WATCH_POINTS_PER_BRIEFING + 1)
        ]
        db_session.add(self._persist(ai_category, watch_points=oversized))
        await db_session.commit()

        with pytest.raises(ValidationError) as exc_info:
            await bff_client.get("/api/v1/briefing/ai")
        assert any(e["type"] == "too_long" for e in exc_info.value.errors())

    @pytest.mark.asyncio
    async def test_oversize_statement_rejected_by_response_model(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
    ) -> None:
        """1 件の statement が上限超なら共有 read で ValidationError 伝播。"""
        db_session.add(
            self._persist(
                ai_category,
                watch_points=[{"statement": "x" * (MAX_WATCH_POINT_STATEMENT_LEN + 1)}],
            )
        )
        await db_session.commit()

        with pytest.raises(ValidationError) as exc_info:
            await bff_client.get("/api/v1/briefing/ai")
        assert any(e["type"] == "string_too_long" for e in exc_info.value.errors())

    @pytest.mark.asyncio
    async def test_oversize_chapters_rejected_by_response_model(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
    ) -> None:
        """chapters 数が上限超なら共有 read で ValidationError 伝播 (本番 500)。"""
        oversized = [
            {"heading": f"h{i}", "body": f"b{i}"}
            for i in range(MAX_CHAPTERS_PER_BRIEFING + 1)
        ]
        db_session.add(self._persist(ai_category, chapters=oversized))
        await db_session.commit()

        with pytest.raises(ValidationError) as exc_info:
            await bff_client.get("/api/v1/briefing/ai")
        assert any(e["type"] == "too_long" for e in exc_info.value.errors())

    @pytest.mark.asyncio
    async def test_oversize_chapter_body_rejected_by_response_model(
        self,
        bff_client: AsyncClient,
        db_session: AsyncSession,
        ai_category: Category,
    ) -> None:
        """1 章の body が上限超なら共有 read で ValidationError 伝播。"""
        db_session.add(
            self._persist(
                ai_category,
                chapters=[{"heading": "h", "body": "x" * (MAX_CHAPTER_BODY_LEN + 1)}],
            )
        )
        await db_session.commit()

        with pytest.raises(ValidationError) as exc_info:
            await bff_client.get("/api/v1/briefing/ai")
        assert any(e["type"] == "string_too_long" for e in exc_info.value.errors())


class TestBriefingSummarySchema:
    """BriefingSummary スキーマ単体: 契約1の required field 強制を確認する。

    FastAPI response_model 経由では未実装 field は silent drop されるため、
    schema を直接構築して required 不変条件 (デフォルト無し・詰め忘れは構築時エラー)
    をテストする。
    """

    def test_requires_key_articles_field(self) -> None:
        """key_articles 無しで構築すると ValidationError (required・デフォルト無し)。"""
        from app.insights.briefing.schemas import BriefingSummary

        with pytest.raises(ValidationError) as exc_info:
            BriefingSummary.model_validate(
                {
                    "week_start": "2026-04-20",
                    "headline": "h",
                    "summary": "s",
                    "input_article_count": 1,
                    "watch_points": [],
                    # key_articles 欠落
                }
            )
        # _CamelBase の alias_generator=to_camel により missing エラーの loc は
        # camelCase alias で報告される。
        assert any(
            e["type"] == "missing" and e["loc"] == ("keyArticles",)
            for e in exc_info.value.errors()
        )

    def test_requires_watch_points_field(self) -> None:
        """watch_points 無しで構築すると ValidationError (required・デフォルト無し)。"""
        from app.insights.briefing.schemas import BriefingSummary

        with pytest.raises(ValidationError) as exc_info:
            BriefingSummary.model_validate(
                {
                    "week_start": "2026-04-20",
                    "headline": "h",
                    "summary": "s",
                    "input_article_count": 1,
                    "key_articles": [],
                    # watch_points 欠落
                }
            )
        # _CamelBase の alias_generator=to_camel により missing エラーの loc は
        # camelCase alias で報告される。
        assert any(
            e["type"] == "missing" and e["loc"] == ("watchPoints",)
            for e in exc_info.value.errors()
        )

    def test_summary_key_article_embeds_article_brief(self) -> None:
        """一覧の keyArticles[].article は ArticleBrief そのもの (カード契約と連動)。

        トップ記事を PaperArticleCard でそのまま描画する前提を、詳細
        (_BriefingDetailKeyArticle) とは別クラス + article: ArticleBrief という
        構造で固定する。significance / 件数の上限は詳細側と共有する。
        """
        from app.insights.briefing.schemas import BriefingDetail, BriefingSummary
        from app.schemas.articles import ArticleBrief

        assert "key_articles" in BriefingSummary.model_fields
        summary_field = BriefingSummary.model_fields["key_articles"]
        detail_field = BriefingDetail.model_fields["key_articles"]
        # keyArticles 件数ガード (MAX_KEY_ARTICLES_PER_BRIEFING) は詳細と共有
        assert summary_field.metadata == detail_field.metadata
        (summary_item,) = get_args(summary_field.annotation)
        (detail_item,) = get_args(detail_field.annotation)
        # consumer が違うので wrapper class は詳細と分ける (payload 差を名前で明示)
        assert summary_item is not detail_item
        assert summary_item.model_fields["article"].annotation is ArticleBrief
        assert (
            summary_item.model_fields["significance"].metadata
            == detail_item.model_fields["significance"].metadata
        )

    def test_watch_points_shares_detail_guard(self) -> None:
        """watchPoints の件数・per-item ガードが詳細とズレないことを固定する。"""
        from app.insights.briefing.schemas import BriefingDetail, BriefingSummary

        assert "watch_points" in BriefingSummary.model_fields
        summary_field = BriefingSummary.model_fields["watch_points"]
        detail_field = BriefingDetail.model_fields["watch_points"]
        assert summary_field.metadata == detail_field.metadata
        # inner の FieldInfo は等値比較できないため Annotated args の metadata
        # 同士で比較する。
        (summary_item,) = get_args(summary_field.annotation)
        (detail_item,) = get_args(detail_field.annotation)
        summary_args = get_args(summary_item)
        detail_args = get_args(detail_item)
        assert summary_args[0] is detail_args[0]
        assert summary_args[1].metadata == detail_args[1].metadata
