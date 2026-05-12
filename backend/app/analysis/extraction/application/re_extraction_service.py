"""ReExtractionService — 既存 Article に対する Stage 1 再抽出 orchestrator。

Phase 1B α-1 の clean break に伴い、過去に旧 prompt / 旧 schema で抽出された
``ArticleExtraction`` を新 prompt / 新 schema (surface + raw_type) で
再生成するための CLI 用 Application Service。

責務:

- 1 article = 1 session = 1 transaction (extractor 呼び出しは session 外)
- ``update_idempotent`` で parent UPDATE のみ → CASCADE 連鎖 (analyses /
  rejections / embeddings / watchlist) を構造的に回避
- 1 件単位の retry (exponential backoff, 上限 ``max_retries``)
- ``dry_run=True`` (CLI default) は AI 呼び出しまで実行し commit せず rollback
  (新 prompt の挙動を本番投入前に確認するための「擬似実行」)
- 進捗ログ ``re_extract_progress`` (article_id, entity_count, elapsed_ms) は
  本文 / 翻訳テキストを含めない (再抽出ログを長期保存しても秘匿性が増えないように)
- 集約結果 ``ReExtractionSummary`` で success / failed / skipped を tuple として返却
  (CLI 側で exit code を決定する)

Design notes:

- ``Article`` は既存 ``ReadyForExtraction`` を経由しない (再抽出対象は既に
  ``ArticleExtraction`` を持つので Pattern A' の precondition「未生成」と矛盾する)。
  本サービスは fetch を内部で行い、無い article_id は ``skipped`` に集約する。
- extractor は呼び出し側で構築 (Pure DI / composition root pattern):
  feedback_pure_di_composition_root.md
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import perf_counter

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.analysis.errors import AnalysisDomainError, InvalidInputError
from app.analysis.extraction.ai.base import BaseExtractor
from app.analysis.extraction.repository import ExtractionRepository
from app.models.article import Article
from app.models.article_extraction import ArticleExtraction

logger = structlog.get_logger(__name__)

_DEFAULT_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class ReExtractionSummary:
    """1 回分の再抽出実行結果。

    - ``success_ids``: 再抽出 + 永続化 (dry_run=True なら rollback 直前の commit
      候補) に成功
    - ``failed_ids``: ``max_retries`` 回 retry しても AnalysisDomainError が再現した
    - ``skipped_ids``: Article 不在 / 既存 ArticleExtraction 不在 / InvalidInputError
    - ``dry_run``: ``True`` の場合は永続化していない (rollback 済み)
    """

    success_ids: tuple[int, ...]
    failed_ids: tuple[int, ...]
    skipped_ids: tuple[int, ...]
    dry_run: bool


class ReExtractionService:
    """既存 Article に対する Stage 1 再抽出ユースケースの orchestrator。

    1 article ごとに 1 transaction を張り、`update_idempotent` で
    parent ``ArticleExtraction`` を UPDATE のみで差し替える (子テーブル
    ``article_extraction_entities`` は DELETE → INSERT)。
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
        article_ids: tuple[int, ...],
        extractor: BaseExtractor,
        *,
        dry_run: bool,
    ) -> ReExtractionSummary:
        """指定 article_id 群を 1 件ずつ再抽出する。

        - 順次実行 (Gemini RPM=100 / RPD クォータを使い切らないため、CLI 側で
          ``--limit`` と組み合わせて batch 制御する)
        - 各 article は独立 transaction (片方が失敗しても他に影響しない)
        - dry_run=True は ``session.rollback()`` で永続化を抑止する
          (extractor の API は実際に叩く: 新 prompt の挙動確認が目的)
        """
        success: list[int] = []
        failed: list[int] = []
        skipped: list[int] = []

        for article_id in article_ids:
            outcome = await self._run_one(
                article_id=article_id,
                extractor=extractor,
                dry_run=dry_run,
            )
            if outcome == "success":
                success.append(article_id)
            elif outcome == "failed":
                failed.append(article_id)
            else:
                skipped.append(article_id)

        return ReExtractionSummary(
            success_ids=tuple(success),
            failed_ids=tuple(failed),
            skipped_ids=tuple(skipped),
            dry_run=dry_run,
        )

    async def _run_one(
        self,
        *,
        article_id: int,
        extractor: BaseExtractor,
        dry_run: bool,
    ) -> str:
        """1 article を再抽出する。"success" / "failed" / "skipped" を返す。"""
        async with self._session_factory() as session:
            article = await self._fetch_article(session, article_id)
            if article is None:
                logger.warning("re_extract_skip_no_article", article_id=article_id)
                return "skipped"
            if not await self._extraction_exists(session, article_id):
                # 再抽出は既存 extraction の差し替えが目的。新規生成は通常 pipeline
                # に任せる責務分離 (CLI で orphan articles を抽出しない)。
                logger.warning(
                    "re_extract_skip_no_existing_extraction", article_id=article_id
                )
                return "skipped"

            title = article.original_title
            content = article.original_content

        # extractor 呼び出しは session 外 (slow IO 中の DB 接続専有を避ける)
        try:
            result = await self._extract_with_retry(
                extractor, title=title, content=content, article_id=article_id
            )
        except InvalidInputError:
            # 本文が AI から見て短すぎる / 解析不能。通常 pipeline でも skip
            # 扱いされるカテゴリのため failed には入れない。
            logger.warning("re_extract_invalid_input", article_id=article_id)
            return "skipped"
        except AnalysisDomainError as exc:
            logger.error(
                "re_extract_failed",
                article_id=article_id,
                error=type(exc).__name__,
            )
            return "failed"

        started = perf_counter()
        async with self._session_factory() as session:
            repo = ExtractionRepository(session)
            updated = await repo.update_idempotent(
                result, article_id=article_id, ai_model=extractor.model_name
            )
            if dry_run:
                await session.rollback()
            else:
                await session.commit()

        elapsed_ms = int((perf_counter() - started) * 1000)
        logger.info(
            "re_extract_progress",
            article_id=article_id,
            extraction_id=updated.id,
            entity_count=len(updated.entities),
            elapsed_ms=elapsed_ms,
            dry_run=dry_run,
        )
        return "success"

    async def _extract_with_retry(
        self,
        extractor: BaseExtractor,
        *,
        title: str,
        content: str,
        article_id: int,
    ):
        """exponential backoff で extractor を最大 ``max_retries`` 回呼び出す。

        ``InvalidInputError`` は回復しない種類のエラーなので即座に伝播。
        他の ``AnalysisDomainError`` (rate-limit / transient API failure) のみ retry。
        """
        last_exc: AnalysisDomainError | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return await extractor.extract(title=title, content=content)
            except InvalidInputError:
                raise
            except AnalysisDomainError as exc:
                last_exc = exc
                logger.warning(
                    "re_extract_retry",
                    article_id=article_id,
                    attempt=attempt,
                    error=type(exc).__name__,
                )
                if attempt >= self._max_retries:
                    break
                await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        assert last_exc is not None  # noqa: S101
        raise last_exc

    @staticmethod
    async def _fetch_article(session: AsyncSession, article_id: int) -> Article | None:
        stmt = select(Article).where(Article.id == article_id)
        return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    async def _extraction_exists(session: AsyncSession, article_id: int) -> bool:
        stmt = (
            select(ArticleExtraction.id)
            .where(ArticleExtraction.article_id == article_id)
            .limit(1)
        )
        return (await session.execute(stmt)).first() is not None
