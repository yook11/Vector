"""WeeklyBriefingService.execute の主要パステスト (LLM mock)。"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import httpx  # noqa: TID251 (テスト内 mock 構築のため、実通信なし)
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.stages.briefing import BriefingOutcomeCode
from app.insights.briefing.domain.briefing import (
    BriefingChapter,
    KeyArticle,
    WatchPoint,
    WeeklyBriefingContent,
)
from app.insights.briefing.domain.ready import BriefingArticle, ReadyForBriefing
from app.insights.briefing.repository import BriefingRepository
from app.insights.briefing.service import (
    BriefingConflict,
    GeneratedBriefing,
    WeeklyBriefingService,
)
from app.models.category import Category
from app.models.pipeline_event import PipelineEvent
from app.models.weekly_briefing import WeeklyBriefing
from app.shared.revalidate import (
    FrontendRevalidateNotifier,
    NullRevalidateNotifier,
)


def _factory_for(db_session) -> async_sessionmaker:
    """テスト中の db_session をそのまま返す session_factory (Service 注入用)。"""
    return async_sessionmaker(
        db_session.bind, class_=AsyncSession, expire_on_commit=False
    )


def _llm_mock(
    headline: str = "今週のハイライト",
    summary: str = "今週の総括",
    chapter_heading: str = "資金とインフラ",
    chapter_body: str = "今週の流れ",
) -> MagicMock:
    llm = MagicMock()
    llm.MODEL = "gemini-3.8-flash"
    llm.generate = AsyncMock(
        return_value=WeeklyBriefingContent(
            headline=headline,
            summary=summary,
            chapters=[BriefingChapter(heading=chapter_heading, body=chapter_body)],
            key_articles=[KeyArticle(analyzed_article_id=1, significance="なぜ重要か")],
            watch_points=[WatchPoint(statement="今後どこを見るべきか")],
        )
    )
    return llm


def _ready(category: Category) -> ReadyForBriefing:
    return ReadyForBriefing(
        week_start=date(2026, 4, 20),
        category_id=category.id,
        category_slug=str(category.slug),
        category_name=str(category.name),
        articles=(
            BriefingArticle(
                analyzed_article_id=1, translated_title="記事", summary="要約"
            ),
        ),
    )


@pytest.fixture
async def ai_category(db_session) -> Category:
    cat = Category(slug="ai", name="AI")
    db_session.add(cat)
    await db_session.commit()
    await db_session.refresh(cat)
    return cat


class TestExecute:
    @pytest.mark.asyncio
    async def test_persists_briefing_when_articles_present(
        self,
        db_session,
        ai_category: Category,
    ) -> None:
        llm = _llm_mock(headline="OK", summary="SUMMARY", chapter_body="BODY")
        service = WeeklyBriefingService(
            _factory_for(db_session), llm, NullRevalidateNotifier()
        )
        ready = _ready(ai_category)

        outcome = await service.execute(ready)

        assert outcome == GeneratedBriefing(
            week_start=date(2026, 4, 20), category_id=ai_category.id, article_count=1
        )
        # LLM には Ready がそろえたカテゴリ名と素材の記事をそのまま渡す。
        llm.generate.assert_awaited_once_with(
            category_name="AI", week_start=date(2026, 4, 20), articles=ready.articles
        )

        # DB 上に 1 行入っていること
        repo = BriefingRepository(db_session)
        saved = await repo.find_by(
            week_start=date(2026, 4, 20), category_id=ai_category.id
        )
        assert saved is not None
        assert saved.headline == "OK"
        assert saved.summary == "SUMMARY"
        assert saved.chapters == [{"heading": "資金とインフラ", "body": "BODY"}]
        assert saved.input_article_count == 1
        assert saved.model_name == "gemini-3.8-flash"
        # key_articles の永続形は {analyzed_article_id, significance} (新形)。
        # _llm_mock の KeyArticle ID がそのまま永続化される。
        assert saved.key_articles == [
            {"analyzed_article_id": 1, "significance": "なぜ重要か"}
        ]

    @pytest.mark.asyncio
    async def test_race_loser_returns_conflict_and_does_not_overwrite_winner(
        self,
        db_session,
        ai_category: Category,
    ) -> None:
        """race 敗北: BriefingConflict が返り、勝者行が上書きされない。

        先に WeeklyBriefing 行を INSERT (= 他 worker の勝利を模擬) してから
        execute を呼ぶ。save() が on_conflict_do_nothing で None を返すため
        BriefingConflict になり、勝者行の headline は変わらない。
        """
        # 他 worker が先行 INSERT した行 (勝者)
        winner_row = WeeklyBriefing(
            week_start_date=date(2026, 4, 20),
            category_id=ai_category.id,
            headline="winner-headline",
            summary="winner-summary",
            chapters=[],
            key_articles=[],
            watch_points=[],
            model_name="gemini-3.8-flash",
            input_article_count=1,
        )
        db_session.add(winner_row)
        await db_session.commit()

        llm = _llm_mock(headline="loser-headline", summary="loser-summary")
        service = WeeklyBriefingService(
            _factory_for(db_session), llm, NullRevalidateNotifier()
        )
        # race は Ready 判定後に他 worker が INSERT した状況なので Ready を直接作る。
        ready = _ready(ai_category)

        outcome = await service.execute(ready)

        assert isinstance(outcome, BriefingConflict)
        assert outcome.week_start == date(2026, 4, 20)
        assert outcome.category_id == ai_category.id
        assert outcome.article_count == 1  # Ready の素材の記事数と一致

        # 勝者行が上書きされていない
        repo = BriefingRepository(db_session)
        stored = await repo.find_by(
            week_start=date(2026, 4, 20), category_id=ai_category.id
        )
        assert stored is not None
        assert stored.headline == "winner-headline"

    @pytest.mark.asyncio
    async def test_propagates_llm_exception(
        self,
        db_session,
        ai_category: Category,
    ) -> None:
        llm = MagicMock()
        llm.MODEL = "gemini-3.8-flash"
        llm.generate = AsyncMock(side_effect=RuntimeError("LLM failed"))
        service = WeeklyBriefingService(
            _factory_for(db_session), llm, NullRevalidateNotifier()
        )
        ready = _ready(ai_category)

        with pytest.raises(RuntimeError, match="LLM failed"):
            await service.execute(ready)


class TestNotifierIntegration:
    @pytest.mark.asyncio
    async def test_calls_notifier_after_successful_persist(
        self,
        db_session,
        ai_category: Category,
    ) -> None:
        notifier = MagicMock()
        notifier.notify = AsyncMock(return_value=None)
        llm = _llm_mock()
        service = WeeklyBriefingService(_factory_for(db_session), llm, notifier)
        ready = _ready(ai_category)

        await service.execute(ready)

        notifier.notify.assert_awaited_once_with(tags=["briefing:ai", "briefing:list"])

    @pytest.mark.asyncio
    async def test_does_not_call_notifier_on_race_loss(
        self,
        db_session,
        ai_category: Category,
    ) -> None:
        """race 敗北時は notifier.notify を呼ばない。

        勝者行が既に INSERT されている状態で execute を呼ぶと BriefingConflict に
        なり、revalidate 通知は勝者プロセスが担うため敗者は呼んではならない。
        """
        winner_row = WeeklyBriefing(
            week_start_date=date(2026, 4, 20),
            category_id=ai_category.id,
            headline="winner-headline",
            summary="winner-summary",
            chapters=[],
            key_articles=[],
            watch_points=[],
            model_name="gemini-3.8-flash",
            input_article_count=1,
        )
        db_session.add(winner_row)
        await db_session.commit()

        notifier = MagicMock()
        notifier.notify = AsyncMock(return_value=None)
        service = WeeklyBriefingService(_factory_for(db_session), _llm_mock(), notifier)
        ready = _ready(ai_category)

        outcome = await service.execute(ready)

        assert isinstance(outcome, BriefingConflict)
        notifier.notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_service_persists_even_when_frontend_revalidate_http_error(
        self,
        db_session,
        ai_category: Category,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """revalidate HTTP 失敗は briefing 生成成功を失敗扱いにしない。

        no-raise 契約は ``FrontendRevalidateNotifier`` が担う。Service 経由でも
        保存成功後に通知を試み、HTTP error が warn 降格されることを固定する。
        """
        captured: list[tuple[str, str, str, bytes]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(
                (
                    request.method,
                    str(request.url),
                    request.headers["authorization"],
                    request.read(),
                )
            )
            return httpx.Response(500, json={"error": "boom"})

        transport = httpx.MockTransport(handler)
        original_init = httpx.AsyncClient.__init__

        def patched_init(
            self: httpx.AsyncClient, *args: object, **kwargs: object
        ) -> None:
            kwargs["transport"] = transport
            original_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

        notifier = FrontendRevalidateNotifier(
            frontend_base_url="http://frontend:3000",
            secret_provider=AsyncMock(
                return_value=SecretStr("test-secret-32characters-long-xxxx")
            ),
        )
        llm = _llm_mock(headline="OK", summary="SUMMARY")
        service = WeeklyBriefingService(_factory_for(db_session), llm, notifier)
        ready = _ready(ai_category)

        outcome = await service.execute(ready)

        assert isinstance(outcome, GeneratedBriefing)
        assert captured == [
            (
                "POST",
                "http://frontend:3000/api/internal/revalidate",
                "Bearer test-secret-32characters-long-xxxx",
                b'{"tags":["briefing:ai","briefing:list"]}',
            )
        ]

        repo = BriefingRepository(db_session)
        saved = await repo.find_by(
            week_start=date(2026, 4, 20), category_id=ai_category.id
        )
        assert saved is not None
        assert saved.headline == "OK"
        assert saved.summary == "SUMMARY"


class TestAuditIntegration:
    """SUCCEEDED audit を briefing 行と同じ tx で書く検証。"""

    @pytest.mark.asyncio
    async def test_writes_succeeded_audit_alongside_briefing(
        self,
        db_session,
        ai_category: Category,
    ) -> None:
        """成功時に briefing 行と SUCCEEDED audit 行が同時に観測される。

        Service が write tx 内で audit を append し commit するため、SUCCEEDED 行は
        briefing UPSERT と atomic (D5)。「briefing 行はあるが SUCCEEDED 無し」の
        偽ギャップが構造的に発生しない。
        """
        service = WeeklyBriefingService(
            _factory_for(db_session), _llm_mock(), NullRevalidateNotifier()
        )
        ready = _ready(ai_category)

        await service.execute(ready)

        rows = (
            (
                await db_session.execute(
                    select(PipelineEvent).where(
                        PipelineEvent.outcome_code
                        == BriefingOutcomeCode.GENERATION_COMPLETED.value
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        ev = rows[0]
        assert ev.stage == "briefing"
        assert ev.event_type == "succeeded"
        assert ev.retryability is None
        assert ev.payload["category_id"] == ai_category.id
        assert ev.payload["category_slug"] == "ai"
        assert ev.payload["article_count"] == 1
        assert ev.payload["ai_model"] == "gemini-3.8-flash"

    @pytest.mark.asyncio
    async def test_does_not_write_succeeded_audit_on_race_loss(
        self,
        db_session,
        ai_category: Category,
    ) -> None:
        """race 敗北時は SUCCEEDED audit を焼かない。

        SUCCEEDED (BriefingOutcomeCode.GENERATION_COMPLETED) は勝者だけが書く設計。
        敗者は save() が None を返した後に audit append をスキップするため、
        DB 上の SUCCEEDED 行は 0 件でなければならない。
        """
        winner_row = WeeklyBriefing(
            week_start_date=date(2026, 4, 20),
            category_id=ai_category.id,
            headline="winner-headline",
            summary="winner-summary",
            chapters=[],
            key_articles=[],
            watch_points=[],
            model_name="gemini-3.8-flash",
            input_article_count=1,
        )
        db_session.add(winner_row)
        await db_session.commit()

        service = WeeklyBriefingService(
            _factory_for(db_session), _llm_mock(), NullRevalidateNotifier()
        )
        ready = _ready(ai_category)

        outcome = await service.execute(ready)

        assert isinstance(outcome, BriefingConflict)
        rows = (
            (
                await db_session.execute(
                    select(PipelineEvent).where(
                        PipelineEvent.outcome_code
                        == BriefingOutcomeCode.GENERATION_COMPLETED.value
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 0
