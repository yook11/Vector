"""WeeklyBriefingService — 1 カテゴリ × 1 週の briefing 生成ユースケース。

Pattern A' での Stage:
- 起動時に ``ReadyForBriefing`` を受け取り、precondition (既存 briefing 判定) は
  Ready 側で吸収済み
- ``execute(ready)`` は articles 取得 → LLM 呼出 → 永続化に専念

3 段階トランザクションパターン (LLM 呼出が 30-60s かかるため):
1. read tx: articles + category 取得
2. LLM 呼出 (no tx): DB connection を占有しない
3. write tx: UPSERT

例外:
- 例外は捕まえずに伝播させる (taskiq の retry / failure tracking に委ねる:
  `feedback_failure_visibility.md`)
- 「articles 0 件」は業務正常状態として ``Outcome.skipped()`` で表現する
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.insights.briefing.application.notifier import BriefingNotifier
from app.insights.briefing.audit_repository import BriefingAuditRepository
from app.insights.briefing.domain.ready import ReadyForBriefing
from app.insights.briefing.llm.deepseek import DeepSeekBriefingGenerator
from app.insights.briefing.repository.articles import BriefingArticleRepository
from app.insights.briefing.repository.briefings import BriefingRepository
from app.models.category import Category
from app.models.weekly_briefing import WeeklyBriefing

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class GeneratedBriefing:
    """``WeeklyBriefingService.execute`` の戻り値。

    ``persisted=False`` は「articles 0 件で生成スキップ」の正常分岐を表す。
    既存 briefing あり + force=False の skip は ``Ready.try_advance_from`` で
    吸収済みのためここには現れない。
    """

    persisted: bool
    week_start: date
    category_id: int
    article_count: int


class WeeklyBriefingService:
    """1 カテゴリ × 1 週の briefing を生成するユースケース。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        llm_generator: DeepSeekBriefingGenerator,
        notifier: BriefingNotifier,
    ) -> None:
        self._session_factory = session_factory
        self._llm = llm_generator
        self._notifier = notifier

    async def execute(self, ready: ReadyForBriefing) -> GeneratedBriefing:
        # --- read tx: articles + category 取得 ---
        async with self._session_factory() as session:
            articles = await BriefingArticleRepository(session).fetch(
                week_start=ready.week_start,
                category_id=ready.category_id,
            )
            category = await session.get(Category, ready.category_id)
        if category is None:
            raise ValueError(f"Category not found: id={ready.category_id}")

        if not articles:
            logger.info(
                "briefing_skip_no_articles",
                week_start=ready.week_start.isoformat(),
                category_id=ready.category_id,
                category_slug=category.slug,
            )
            # 記事ゼロは steady-state 異常系 (D2) — REJECTED で audit に焼く。
            # 読 tx 直後の独立した別 tx で焼く (LLM 呼出も write tx も走らない)。
            async with self._session_factory() as session:
                await BriefingAuditRepository(session).append_input_empty(ready=ready)
                await session.commit()
            return GeneratedBriefing(
                persisted=False,
                week_start=ready.week_start,
                category_id=ready.category_id,
                article_count=0,
            )

        # --- LLM 呼出 (no tx, 30-60s) ---
        content = await self._llm.generate(
            category_name=str(category.name),
            week_start=ready.week_start,
            articles=articles,
        )

        # --- write tx: UPSERT ---
        async with self._session_factory() as session:
            briefing_repo = BriefingRepository(session)
            briefing = WeeklyBriefing(
                week_start_date=ready.week_start,
                category_id=ready.category_id,
                headline=content.headline,
                overview=content.overview,
                stories=[s.model_dump() for s in content.stories],
                model_name=self._llm.MODEL,
                input_article_count=len(articles),
            )
            saved = await briefing_repo.save(briefing, force=ready.force)
            # audit は INSERT 勝者だけが焼く (saved is None = race 敗北は沈黙、
            # 勝者プロセスが SUCCEEDED を 1 行付ける構造で完成行の重複を防ぐ)。
            # 同 tx atomic で「briefing 行はあるが SUCCEEDED 無し」の偽ギャップ
            # を構造的に排除する。
            if saved is not None:
                await BriefingAuditRepository(session).append_completed(
                    ready=ready,
                    article_count=len(articles),
                    ai_model=self._llm.MODEL,
                )
            await session.commit()
            if saved is None:
                # race 敗北 (force=False で他 worker が先行 INSERT): 勝者を読戻す。
                # race-loss modernization は本 PR スコープ外 (Phase B で
                # briefing + snapshot まとめて readback / RuntimeError 撤去)。
                logger.info(
                    "briefing_concurrent_write",
                    week_start=ready.week_start.isoformat(),
                    category_id=ready.category_id,
                )
                saved = await briefing_repo.find_by(
                    week_start=ready.week_start, category_id=ready.category_id
                )
                if saved is None:
                    raise RuntimeError(
                        "briefing_race_winner_missing: "
                        f"week_start={ready.week_start.isoformat()} "
                        f"category_id={ready.category_id}"
                    )

        logger.info(
            "briefing_generated",
            week_start=ready.week_start.isoformat(),
            category_id=ready.category_id,
            category_slug=category.slug,
            article_count=len(articles),
            forced=ready.force,
        )
        # 永続化成功後に frontend のキャッシュ無効化を通知する。
        # notifier 内部で warn 降格するため例外は伝播しない。
        await self._notifier.notify(category_slug=str(category.slug))
        return GeneratedBriefing(
            persisted=True,
            week_start=ready.week_start,
            category_id=ready.category_id,
            article_count=len(articles),
        )
