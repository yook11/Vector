"""RecurationService — 既存 article record の Stage 3 再 curation CLI helper。

既存 ``ArticleCuration`` を現在の curation 仕様で再生成する保守 CLI 用サービス。

責務:

- 1 article = 1 session = 1 transaction (curator 呼び出しは session 外)
- ``update_signal_idempotent`` で parent UPDATE のみ → CASCADE 連鎖 (analyses /
  rejections / embeddings / watchlist) を構造的に回避
- 1 件単位の retry (exponential backoff, 上限 ``max_retries``)
- ``dry_run=True`` (CLI default) は AI 呼び出しまで実行し commit せず rollback
  (新 prompt の挙動を本番投入前に確認するための「擬似実行」)
- 進捗ログ ``re_curate_progress`` (analyzable_article_id, entity_count, elapsed_ms) は
  本文 / 翻訳テキストを含めない (再 curation ログを長期保存しても秘匿性が増えないように)
- 集約結果 ``RecurationSummary`` で success / failed / skipped を tuple として返却
  (CLI 側で exit code を決定する)

設計メモ:

- ``AnalyzableArticleRecord`` は既存 ``ReadyForCuration`` を経由しない。
  再 curation 対象は既に ``ArticleCuration`` を持つので Pattern A' の
  precondition「未生成」と矛盾する。
  本サービスは fetch を内部で行い、無い analyzable_article_id は ``skipped`` に
  集約する。
- curator は呼び出し側で構築する。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import perf_counter

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.ai_provider_errors import AIProviderError
from app.analysis.curation.ai.base import BaseCurator
from app.analysis.curation.ai.envelope import CurationCall
from app.analysis.curation.domain import Noise, Signal
from app.analysis.curation.errors import (
    CurationRecoverableError,
    CurationTerminalDropError,
    CurationTerminalKeepError,
    map_provider_to_curation,
)
from app.analysis.curation.repository import CurationRepository
from app.models.analyzable_article_record import AnalyzableArticleRecord

logger = structlog.get_logger(__name__)

_DEFAULT_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class RecurationSummary:
    """1 回分の再 curation 実行結果。

    - ``success_ids``: 再 curation + 永続化 (dry_run=True なら rollback 直前の commit
      候補) に成功
    - ``failed_ids``: ``CurationTerminalKeepError`` (Configuration / Balance 等)
      で即失敗、または ``CurationRecoverableError`` が ``max_retries`` 回再現
    - ``skipped_ids``: AnalyzableArticleRecord 不在 / 既存 ArticleCuration 不在 /
      ``CurationTerminalDropError`` (input rejected / output blocked)
    - ``dry_run``: ``True`` の場合は永続化していない (rollback 済み)
    """

    success_ids: tuple[int, ...]
    failed_ids: tuple[int, ...]
    skipped_ids: tuple[int, ...]
    dry_run: bool


class RecurationService:
    """既存 AnalyzableArticleRecord に対する Stage 3 再 curation CLI の処理本体。

    1 article ごとに 1 transaction を張り、`update_signal_idempotent` で
    parent ``ArticleCuration`` を UPDATE のみで差し替える。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> None:
        self._session_factory = session_factory
        self._max_retries = max_retries

    async def execute(
        self,
        analyzable_article_ids: tuple[int, ...],
        curator: BaseCurator,
        *,
        dry_run: bool,
    ) -> RecurationSummary:
        """指定 analyzable_article_id 群を 1 件ずつ再 curation する。

        - 順次実行 (Gemini RPM=100 / RPD クォータを使い切らないため、CLI 側で
          ``--limit`` と組み合わせて batch 制御する)
        - 各 article は独立 transaction (片方が失敗しても他に影響しない)
        - dry_run=True は ``session.rollback()`` で永続化を抑止する
          (curator の API は実際に叩く: 新 prompt の挙動確認が目的)
        """
        success: list[int] = []
        failed: list[int] = []
        skipped: list[int] = []

        for analyzable_article_id in analyzable_article_ids:
            outcome = await self._run_one(
                analyzable_article_id=analyzable_article_id,
                curator=curator,
                dry_run=dry_run,
            )
            if outcome == "success":
                success.append(analyzable_article_id)
            elif outcome == "failed":
                failed.append(analyzable_article_id)
            else:
                skipped.append(analyzable_article_id)

        return RecurationSummary(
            success_ids=tuple(success),
            failed_ids=tuple(failed),
            skipped_ids=tuple(skipped),
            dry_run=dry_run,
        )

    async def _run_one(
        self,
        *,
        analyzable_article_id: int,
        curator: BaseCurator,
        dry_run: bool,
    ) -> str:
        """1 article を再 curation する。"success" / "failed" / "skipped" を返す。"""
        async with self._session_factory() as session:
            article = await self._fetch_article(session, analyzable_article_id)
            if article is None:
                logger.warning(
                    "re_curate_skip_no_article",
                    analyzable_article_id=analyzable_article_id,
                )
                return "skipped"
            if not await CurationRepository(session).signal_exists_for_article(
                analyzable_article_id
            ):
                # 再 curation は既存 curation の差し替えのみを扱う。
                logger.warning(
                    "re_curate_skip_no_existing_curation",
                    analyzable_article_id=analyzable_article_id,
                )
                return "skipped"

            title = article.original_title
            content = article.original_content

        # curator 呼び出しは session 外 (slow IO 中の DB 接続専有を避ける)
        try:
            envelope = await self._curate_with_retry(
                curator,
                title=title,
                content=content,
                analyzable_article_id=analyzable_article_id,
            )
        except CurationTerminalDropError:
            # AI から見て扱えない記事 (input rejected / output blocked)。
            # 通常 pipeline でも記事 DELETE 対象のカテゴリなので skipped 扱い。
            logger.warning(
                "re_curate_drop_article", analyzable_article_id=analyzable_article_id
            )
            return "skipped"
        except CurationTerminalKeepError as exc:
            # Configuration / RequestInvalid / Balance 等。retry しても解消しない
            # ため max_retries を消費せず即 failed (本番 task は keep article で
            # audit + return する)。
            logger.error(
                "re_curate_failed_permanent",
                analyzable_article_id=analyzable_article_id,
                error=type(exc).__name__,
            )
            return "failed"
        except CurationRecoverableError as exc:
            # max_retries 回 retry しても再現した recoverable エラー。
            logger.error(
                "re_curate_failed_after_retry",
                analyzable_article_id=analyzable_article_id,
                error=type(exc).__name__,
            )
            return "failed"

        # ``CurationCall[Signal]`` のみ既存 curation の上書きに進める。
        match envelope:
            case CurationCall(result=Signal()):
                started = perf_counter()
                async with self._session_factory() as session:
                    repo = CurationRepository(session)
                    curation_id = await repo.update_signal_idempotent(
                        envelope, analyzable_article_id=analyzable_article_id
                    )
                    if dry_run:
                        await session.rollback()
                    else:
                        await session.commit()

                elapsed_ms = int((perf_counter() - started) * 1000)
                logger.info(
                    "re_curate_progress",
                    analyzable_article_id=analyzable_article_id,
                    curation_id=curation_id,
                    elapsed_ms=elapsed_ms,
                    dry_run=dry_run,
                )
                return "success"
            case CurationCall(result=Noise()):
                # 既存の signal 抽出に対し再 curation で Noise が返った場合は触らない。
                logger.warning(
                    "re_curate_skipped_noise",
                    analyzable_article_id=analyzable_article_id,
                )
                return "skipped"
            case _:
                # 到達不能 (curator は Signal | Noise の union を返す契約)
                raise RuntimeError(
                    "re_curate_unreachable_envelope_variant: "
                    f"analyzable_article_id={analyzable_article_id}"
                )

    async def _curate_once_mapped(
        self,
        curator: BaseCurator,
        *,
        title: str,
        content: str,
    ) -> CurationCall[Signal] | CurationCall[Noise]:
        """curator を 1 回呼び、provider error を Stage 3 marker に詰め替える。

        同じ try 内で詰め替えと catch を行うと、再 raise した例外は同レベルの
        sibling except に流れないため、boundary をこの helper に分離する。
        """
        try:
            return await curator.curate(title=title, content=content)
        except AIProviderError as exc:
            raise map_provider_to_curation(exc) from exc

    async def _curate_with_retry(
        self,
        curator: BaseCurator,
        *,
        title: str,
        content: str,
        analyzable_article_id: int,
    ) -> CurationCall[Signal] | CurationCall[Noise]:
        """exponential backoff で curator を最大 ``max_retries`` 回呼び出す。

        ``CurationTerminalDropError`` / ``CurationTerminalKeepError`` は
        即時伝播 (retry 無意味)。``CurationRecoverableError`` のみ backoff
        retry する。provider error の詰め替えは ``_curate_once_mapped`` 内で
        完結しているため、本 loop は Stage 3 marker のみ catch する。
        """
        last_exc: CurationRecoverableError | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return await self._curate_once_mapped(
                    curator, title=title, content=content
                )
            except (CurationTerminalDropError, CurationTerminalKeepError):
                raise
            except CurationRecoverableError as exc:
                last_exc = exc
                logger.warning(
                    "re_curate_retry",
                    analyzable_article_id=analyzable_article_id,
                    attempt=attempt,
                    error=type(exc).__name__,
                )
                if attempt >= self._max_retries:
                    break
                await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        assert last_exc is not None  # noqa: S101
        raise last_exc

    @staticmethod
    async def _fetch_article(
        session: AsyncSession, analyzable_article_id: int
    ) -> AnalyzableArticleRecord | None:
        stmt = select(AnalyzableArticleRecord).where(
            AnalyzableArticleRecord.id == analyzable_article_id
        )
        return (await session.execute(stmt)).scalar_one_or_none()
