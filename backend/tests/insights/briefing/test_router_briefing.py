"""GET /api/v1/briefing/{categorySlug} のエンドポイントテスト。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
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
                key_articles=[{"assessment_id": analysis.id, "significance": "なぜ重要か"}],
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
                key_articles=[{"assessment_id": analysis.id, "significance": "なぜ重要か"}],
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
                key_articles=[{"assessment_id": analysis.id, "significance": "なぜ重要か"}],
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
                key_articles=[{"assessment_id": analysis.id, "significance": "なぜ重要か"}],
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
                key_articles=[{"assessment_id": analysis.id, "significance": "なぜ重要か"}],
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
    ) -> None:
        briefing = WeeklyBriefing(
            week_start_date=date(2026, 4, 20),
            category_id=ai_category.id,
            headline="今週のヘッドライン",
            summary="今週の総括リード",
            chapters=[{"heading": "資金とインフラ", "body": "今週の流れの本文"}],
            key_articles=[{"assessment_id": 1, "significance": "なぜ重要か"}],
            watch_points=[{"statement": "今後どこを見るべきか"}],
            model_name="deepseek-v4-pro",
            input_article_count=1,
        )
        db_session.add(briefing)
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
    ) -> None:
        """totalArticles は今週生成された briefing のみ合計し、古い週の stale
        briefing (生成が遅れたカテゴリの latest) は含めない。"""
        current_week = latest_completed_week_start(now_in_jst())
        old_week = current_week - timedelta(days=7)

        robotics = Category(slug="robotics", name="ロボティクス")
        db_session.add(robotics)
        await db_session.commit()
        await db_session.refresh(robotics)

        # ai は今週分 (count=7)、robotics は古い週の stale briefing (count=40)
        seeds = {
            ai_category.id: (current_week, 7),
            robotics.id: (old_week, 40),
        }
        for category_id, (week, count) in seeds.items():
            db_session.add(
                WeeklyBriefing(
                    week_start_date=week,
                    category_id=category_id,
                    headline="h",
                    summary="s",
                    chapters=[{"heading": "h", "body": "b"}],
                    key_articles=[{"assessment_id": 1, "significance": "s"}],
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


class TestBriefingResponseSizeGuard:
    """red-team F10: 共有 read 経路で巨大 briefing JSONB が response として
    流れる経路を構造的に塞ぐ。

    AUTH-N4 / AUTH-C1 経由で attacker が DB に巨大 key_articles / watch_points を
    直書きしたシナリオ。key_articles の件数は router の count guard が embed
    fetch 前に弾き、それ以外は Field(max_length=...) が `_BriefingKeyArticle` /
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
