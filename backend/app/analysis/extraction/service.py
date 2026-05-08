"""Extraction サービス — Stage C の処理組み立てと DB 永続化。

Pattern A' (spec §3.2 / §6.1 / §7) で `ReadyForExtraction` を Stage 間 passport
として受け取り、precondition (Article 存在 + Extraction/Noise 未生成 + 本文
サイズ ≤ hard cap) は型レベルで構造保証されている。本サービスは:

- AI 呼び出し (session 外、slow IO 中の DB 接続専有を排除 — spec §4.7)
- ``relevance`` で signal/noise を振り分け、それぞれ別テーブルへ永続化
- race 敗北時の読戻し → Outcome 返却
- 各 Outcome 経路で同 tx に ``pipeline_events`` audit を焼付 (PR3-a-1)
- 内容起因 Permanent failure 経路で 1 tx 内 audit + article DELETE (PR3-a-1)

の責務に縮退している。Outcome は ``ExtractedOutcome | NoiseOutcome |
InvalidInputOutcome`` の 3 variants。
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.errors import InvalidInputError
from app.analysis.extraction.audit import base_extraction_payload_fields
from app.analysis.extraction.domain import Extraction
from app.analysis.extraction.domain.ready import ReadyForExtraction
from app.analysis.extraction.extractor.base import BaseExtractor
from app.analysis.extraction.extractor.envelope import ExtractionCall
from app.analysis.extraction.noise_repository import NoiseRepository
from app.analysis.extraction.repository import ExtractionRepository
from app.models.article import Article
from app.models.news_source import NewsSource
from app.observability.domain.event import EventType, Stage
from app.observability.domain.payloads import ExtractionPayload
from app.observability.repository import PipelineEventRepository
from app.repositories.articles import ArticleRepository

logger = structlog.get_logger(__name__)

_AI_RAW_RESPONSE_LIMIT = 2048


@dataclass(frozen=True, slots=True)
class ExtractedOutcome:
    """Stage C 成功 (signal、新規 INSERT or race 敗北からの読戻し)。

    下流 Stage D に chain する。
    """

    extraction: Extraction


@dataclass(frozen=True, slots=True)
class NoiseOutcome:
    """Stage C で noise 判定。``extraction_noises`` に永続化済、chain しない。

    payload なし — Service が永続化 + ログ済 (``InvalidInputOutcome`` と同様)。
    """


@dataclass(frozen=True, slots=True)
class InvalidInputOutcome:
    """AI が ``InvalidInputError`` を返した。chain しない。"""


ExtractionOutcome = ExtractedOutcome | NoiseOutcome | InvalidInputOutcome


async def _resolve_source_name(session: AsyncSession, article_id: int) -> str | None:
    """``article_id`` から ``news_sources.name`` を 1 SELECT で引く。

    audit payload の FK 切断耐性のため (article DELETE 後でも source 名で
    トレース可能にする)。``str`` 化して返す (NewsSource.name は VO のため)。
    """
    stmt = (
        select(NewsSource.name)
        .join(Article, Article.source_id == NewsSource.id)
        .where(Article.id == article_id)
    )
    name = await session.scalar(stmt)
    return str(name) if name is not None else None


class ExtractionService:
    """1 記事の事実抽出と結果永続化を行うアトミックなユースケース。

    Stage C: 原文を読み、翻訳タイトル・事実ベース要約・エンティティを抽出する。
    分類（カテゴリ・トピック・インパクト）は Stage D の責務。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(
        self,
        ready: ReadyForExtraction,
        extractor: BaseExtractor,
    ) -> ExtractionOutcome:
        """1 記事に対して事実抽出を実行する。

        precondition は ``ReadyForExtraction`` で構造保証済 (Article 存在 +
        Extraction 未生成 + 本文サイズ ≤ hard cap)。本メソッド内で再 fetch
        / None check は行わない。

        Raises:
            AnalysisDomainError のサブクラス（InvalidInputError を除く）。
            ExtractionPolicyBlockedError / ExtractionInputTooLargeError は
            tasks.py 側で catch して ``mark_article_unprocessable`` を呼ぶ。
        """
        # AI 呼び出しは session 外 (slow IO 中の DB 接続専有を排除)
        try:
            envelope = await extractor.extract(
                title=ready.original_title,
                content=ready.original_content,
            )
        except InvalidInputError as exc:
            logger.warning("extraction_invalid_input", article_id=ready.article_id)
            return await self._persist_invalid_input(ready, exc)

        if envelope.result.relevance == "noise":
            return await self._persist_noise(ready, envelope, extractor.model_name)
        return await self._persist_signal(ready, envelope, extractor.model_name)

    async def _persist_signal(
        self,
        ready: ReadyForExtraction,
        envelope: ExtractionCall,
        ai_model: str,
    ) -> ExtractedOutcome:
        async with self._session_factory() as session:
            repo = ExtractionRepository(session)
            saved = await repo.save(
                envelope.result,
                article_id=ready.article_id,
                ai_model=ai_model,
            )

            if saved is None:
                # race 敗北 — 勝者を読み戻して合流 (audit は勝者側で焼かれる)
                logger.info(
                    "extraction_concurrent_write",
                    article_id=ready.article_id,
                )
                await session.commit()
                async with self._session_factory() as read_session:
                    saved = await ExtractionRepository(read_session).find_by_article_id(
                        ready.article_id
                    )
                if saved is None:
                    raise RuntimeError(
                        f"extraction_race_winner_missing: article_id={ready.article_id}"
                    )
                return ExtractedOutcome(extraction=saved)

            # 同 tx に audit 焼付
            source_name = await _resolve_source_name(session, ready.article_id)
            payload = ExtractionPayload(
                **base_extraction_payload_fields(
                    original_content=ready.original_content,
                    source_name=source_name,
                ),
                ai_raw_response=envelope.raw_response[:_AI_RAW_RESPONSE_LIMIT] or None,
                entity_count=len(envelope.result.entities),
            )
            await PipelineEventRepository(session).append(
                stage=Stage.EXTRACTION,
                event_type=EventType.SUCCEEDED,
                outcome_code="extracted",
                payload=payload,
                article_id=ready.article_id,
            )
            await session.commit()

            logger.info(
                "extraction_completed",
                article_id=ready.article_id,
                extraction_id=saved.id,
                entity_count=len(saved.entities),
            )
            return ExtractedOutcome(extraction=saved)

    async def _persist_noise(
        self,
        ready: ReadyForExtraction,
        envelope: ExtractionCall,
        ai_model: str,
    ) -> NoiseOutcome:
        async with self._session_factory() as session:
            noise_repo = NoiseRepository(session)
            saved = await noise_repo.save(
                envelope.result,
                article_id=ready.article_id,
                ai_model=ai_model,
            )

            if saved is None:
                # UNIQUE 違反による race 敗北 — 勝者を読み戻して合流する
                logger.info(
                    "extraction_noise_concurrent_write",
                    article_id=ready.article_id,
                )
                await session.commit()
                async with self._session_factory() as read_session:
                    saved = await NoiseRepository(read_session).find_by_article_id(
                        ready.article_id
                    )
                if saved is None:
                    raise RuntimeError(
                        f"extraction_noise_race_winner_missing: "
                        f"article_id={ready.article_id}"
                    )
                return NoiseOutcome()

            source_name = await _resolve_source_name(session, ready.article_id)
            payload = ExtractionPayload(
                **base_extraction_payload_fields(
                    original_content=ready.original_content,
                    source_name=source_name,
                ),
                ai_raw_response=envelope.raw_response[:_AI_RAW_RESPONSE_LIMIT] or None,
                entity_count=len(envelope.result.entities),
            )
            await PipelineEventRepository(session).append(
                stage=Stage.EXTRACTION,
                event_type=EventType.SUCCEEDED,
                outcome_code="extracted_as_noise",
                payload=payload,
                article_id=ready.article_id,
            )
            await session.commit()

            logger.info(
                "extraction_noise_recorded",
                article_id=ready.article_id,
                noise_id=saved.id,
                entity_count=len(saved.entities),
            )
            return NoiseOutcome()

    async def _persist_invalid_input(
        self,
        ready: ReadyForExtraction,
        exc: InvalidInputError,
    ) -> InvalidInputOutcome:
        """``InvalidInputError`` 経路の audit のみ。article は残す (人間対応)。"""
        async with self._session_factory() as session:
            source_name = await _resolve_source_name(session, ready.article_id)
            payload = ExtractionPayload(
                **base_extraction_payload_fields(
                    original_content=ready.original_content,
                    source_name=source_name,
                ),
                error_message=str(exc)[:2000] or None,
                error_chain=[f"{type(exc).__module__}.{type(exc).__qualname__}"],
            )
            await PipelineEventRepository(session).append(
                stage=Stage.EXTRACTION,
                event_type=EventType.SKIPPED,
                outcome_code="skipped_invalid_input",
                payload=payload,
                article_id=ready.article_id,
                error_class=f"{type(exc).__module__}.{type(exc).__qualname__}",
            )
            await session.commit()
        return InvalidInputOutcome()

    async def mark_article_unprocessable(
        self,
        article_id: int,
        original_content: str,
        *,
        outcome_code: str,
        exc: BaseException,
    ) -> None:
        """内容起因 Permanent failure を 1 tx で焼付け + 記事 DELETE する。

        順序: **audit INSERT 先、DELETE 後** — A 級保険を最大化し、source_id
        の自動逆引きが Article 存在中に確定するように。FK は ``ondelete=
        SET NULL`` 設定済 (``pipeline_events.article_id``) のため DELETE 後も
        audit 行は残り、``source_id`` で起点ソースを追跡可能。

        Args:
            article_id: 削除対象記事 ID。
            original_content: 削除前に audit に焼く本文 (caller が読み出して
                渡す。Article DELETE 後は読めなくなるため)。
            outcome_code: ``ai_error_blocked_by_policy`` または
                ``ai_error_input_too_large`` (両者とも内容起因 Permanent)。
            exc: 例外インスタンス (audit の error_message / error_chain 用)。
        """
        async with self._session_factory() as session:
            source_name = await _resolve_source_name(session, article_id)
            error_chain = [f"{type(exc).__module__}.{type(exc).__qualname__}"]
            extra_fields: dict[str, object] = {
                "error_message": str(exc)[:2000] or None,
                "error_chain": error_chain,
            }
            # ExtractionPolicyBlockedError は raw_response を保持している
            raw_response = getattr(exc, "raw_response", None)
            if isinstance(raw_response, str) and raw_response:
                extra_fields["ai_raw_response"] = raw_response[:_AI_RAW_RESPONSE_LIMIT]

            payload = ExtractionPayload(
                **base_extraction_payload_fields(
                    original_content=original_content,
                    source_name=source_name,
                ),
                **extra_fields,  # type: ignore[arg-type]
            )

            # 1) audit INSERT (source_id 自動補完が article_id 健在時に確定)
            await PipelineEventRepository(session).append(
                stage=Stage.EXTRACTION,
                event_type=EventType.FAILED,
                outcome_code=outcome_code,
                payload=payload,
                article_id=article_id,
                error_class=f"{type(exc).__module__}.{type(exc).__qualname__}",
            )

            # 2) article DELETE (CASCADE で関連 row、SET NULL で audit.article_id)
            deleted = await ArticleRepository(session).delete_by_id(article_id)
            await session.commit()

            logger.warning(
                "extraction_article_unprocessable",
                article_id=article_id,
                outcome_code=outcome_code,
                deleted_rows=deleted,
                error_class=f"{type(exc).__module__}.{type(exc).__qualname__}",
            )
