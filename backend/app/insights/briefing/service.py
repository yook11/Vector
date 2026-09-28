"""WeeklyBriefingService — 1 カテゴリ × 1 週の briefing 生成ユースケース。

開始条件と素材の記事は ``ReadyForBriefing`` がそろえて渡すため、ここでは
LLM 呼出 → 永続化 → 通知だけを行う。LLM 呼出は 30-60s かかるため
トランザクションの外で行い、書き込みだけを 1 トランザクションにまとめる。

例外は捕まえずに伝播させ、taskiq の retry と失敗監査に委ねる。
同時 INSERT の競合に負けた場合は読み戻さず ``BriefingConflict`` を返す。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.stages.briefing import BriefingAuditRepository
from app.insights.briefing.domain.ready import ReadyForBriefing
from app.insights.briefing.repository import BriefingRepository

if TYPE_CHECKING:
    # 具象 generator は composition root (broker_briefing hook) が構築し DI で渡す。
    # 型注釈専用 import に降格し、本 module 経由で openai SDK が import 時にロード
    # されるのを防ぐ (app/queue/composition.py の遅延 SDK import 方針と対)。
    from app.insights.briefing.llm import DeepSeekBriefingGenerator
    from app.shared.revalidate import RevalidateNotifier

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class GeneratedBriefing:
    """briefing を生成して保存した。"""

    week_start: date
    category_id: int
    article_count: int


@dataclass(frozen=True, slots=True)
class BriefingConflict:
    """同時実行により別 worker が先に保存したため、自 worker は保存しなかった。"""

    week_start: date
    category_id: int
    article_count: int


BriefingOutcome = GeneratedBriefing | BriefingConflict


class WeeklyBriefingService:
    """1 カテゴリ × 1 週の briefing を生成するユースケース。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        llm_generator: DeepSeekBriefingGenerator,
        notifier: RevalidateNotifier,
    ) -> None:
        self._session_factory = session_factory
        self._llm = llm_generator
        self._notifier = notifier

    async def execute(self, ready: ReadyForBriefing) -> BriefingOutcome:
        article_count = len(ready.articles)

        # --- LLM 呼出 (no tx, 30-60s) ---
        content = await self._llm.generate(
            category_name=ready.category_name,
            week_start=ready.week_start,
            articles=ready.articles,
        )

        # --- write tx: INSERT ---
        async with self._session_factory() as session:
            saved = await BriefingRepository(session).save(
                content,
                week_start=ready.week_start,
                category_id=ready.category_id,
                model_name=self._llm.MODEL,
                input_article_count=article_count,
            )
            # audit は INSERT 勝者だけが焼く (saved is None = race 敗北は沈黙、
            # 勝者プロセスが SUCCEEDED を 1 行付ける構造で完成行の重複を防ぐ)。
            # 同 tx atomic で「briefing 行はあるが SUCCEEDED 無し」の偽ギャップ
            # を構造的に排除する。
            if saved is not None:
                await BriefingAuditRepository(session).append_generation_completed(
                    week_start=ready.week_start,
                    category_id=ready.category_id,
                    article_count=article_count,
                    ai_model=self._llm.MODEL,
                )
            await session.commit()

        if saved is None:
            # race 敗北 (他 worker が先行 INSERT): 読み戻さず
            # Conflict を返す。revalidate 通知は勝者側が行う。
            logger.info(
                "briefing_concurrent_write",
                week_start=ready.week_start.isoformat(),
                category_id=ready.category_id,
            )
            return BriefingConflict(
                week_start=ready.week_start,
                category_id=ready.category_id,
                article_count=article_count,
            )

        logger.info(
            "briefing_generated",
            week_start=ready.week_start.isoformat(),
            category_id=ready.category_id,
            category_slug=ready.category_slug,
            article_count=article_count,
        )
        # 永続化成功後に frontend のキャッシュ無効化を通知する。tag は frontend の
        # lib/cache/tags.ts と一致させる。notifier 内部で warn 降格するため
        # 例外は伝播しない。
        await self._notifier.notify(
            tags=[f"briefing:{ready.category_slug}", "briefing:list"]
        )
        return GeneratedBriefing(
            week_start=ready.week_start,
            category_id=ready.category_id,
            article_count=article_count,
        )
