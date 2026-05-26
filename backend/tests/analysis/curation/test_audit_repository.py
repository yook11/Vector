"""``CurationAuditRepository`` の semantic method 単独テスト (PR3.5-c)。

audit row の shape SSoT が repository に集約されたことを検証する:

- ``append_signal`` / ``append_noise`` で
  ``category=success`` + ``code`` (caller 渡し) / ``outcome_code=code`` が記録
- ``append_drop_article`` で
  ``category=non_retryable_drop_article`` + ``code=exc.code`` (Stage 3 marker
  の instance attr。ACL が provider ``CODE`` を引き継ぐ)
- ``append_failure`` で **Stage 3 marker 型による dispatch** が動作:
  - ``CurationTerminalDropError`` → ``category=non_retryable_drop_article``
  - ``CurationTerminalKeepError`` → ``category=non_retryable_keep_article``
  - ``CurationRecoverableError`` → ``category=retryable``
  - 想定外 ``RuntimeError`` → ``category=unknown`` / ``code=unexpected_error``
- repository は ``commit`` を呼ばない (caller の tx 境界保持)
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import (
    IntegrityError,
    InvalidRequestError,
    OperationalError,
    ProgrammingError,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderInputRejectedError,
    AIProviderNetworkError,
    AIProviderOutputBlockedError,
)
from app.analysis.curation.ai.base import BaseCurator
from app.analysis.curation.ai.envelope import CurationCall
from app.analysis.curation.ai.gemini_spec import GEMINI_CURATION_SPEC
from app.analysis.curation.domain import Noise, Signal
from app.analysis.curation.domain.ready import ReadyForCuration
from app.analysis.curation.errors import (
    CurationResponseInvalidError,
    map_provider_to_curation,
)
from app.audit.stages.curation import CurationAuditRepository
from app.models.article import Article
from app.models.news_source import NewsSource
from app.models.pipeline_event import PipelineEvent


def _curator_mock(
    *,
    model: str = "test-extract-model",
    prompt_version: str = "test-extract-prompt-v1",
) -> MagicMock:
    """失敗 audit テスト用の ``BaseCurator`` mock。

    PR4 で ``BaseCurator`` の構造保証は property 契約 (model_name /
    prompt_version / rate_policy) に置き換わったため、property 属性として
    値を bind する。値は test-* で Gemini と衝突しない名前にする。
    """
    mock = MagicMock(spec=BaseCurator)
    type(mock).model_name = model
    type(mock).prompt_version = prompt_version
    return mock


def _signal_envelope() -> CurationCall[Signal]:
    return CurationCall(
        result=Signal(title_ja="日本語タイトル", summary_ja="日本語要約"),
        raw_response='{"relevance":"signal"}',
        raw_relevance="signal",
        prompt_version=GEMINI_CURATION_SPEC.version,
        model_name=GEMINI_CURATION_SPEC.model,
    )


def _noise_envelope() -> CurationCall[Noise]:
    return CurationCall(
        result=Noise(title_ja="日本語タイトル", summary_ja="日本語要約"),
        raw_response='{"relevance":"noise"}',
        raw_relevance="noise",
        prompt_version=GEMINI_CURATION_SPEC.version,
        model_name=GEMINI_CURATION_SPEC.model,
    )


async def _make_article(
    db_session: AsyncSession, sample_source: NewsSource, *, content: str = "body x" * 30
) -> Article:
    article = Article(
        source_id=sample_source.id,
        source_url="https://e.com/a",  # type: ignore[arg-type]
        original_title="t",
        original_content=content,
        published_at=datetime.now(UTC),
    )
    db_session.add(article)
    await db_session.commit()
    await db_session.refresh(article)
    return article


def _ready(article: Article) -> ReadyForCuration:
    return ReadyForCuration(
        article_id=article.id,
        original_title=article.original_title,
        original_content=article.original_content,
    )


async def _fetch_one(db_session: AsyncSession, article_id: int) -> PipelineEvent:
    rows = (
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.article_id == article_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    return rows[0]


# ---------------------------------------------------------------------------
# 成功経路 — append_signal / append_noise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_signal_records_success_with_code(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """signal 経路で category=success / code=curated_signal が記録される。"""
    article = await _make_article(db_session, sample_source)
    async with session_factory() as session:
        await CurationAuditRepository(session).append_signal(
            ready=_ready(article),
            envelope=_signal_envelope(),
            code="curated_signal",
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == "curated_signal"
    assert ev.category == "success"
    assert ev.code == "curated_signal"
    assert ev.payload["ai_raw_response"]
    assert ev.payload["source_name"] == str(sample_source.name)
    # PR1-a: ai_model / prompt_version / raw_relevance は envelope 経由で焼かれる
    assert ev.payload["ai_model"] == GEMINI_CURATION_SPEC.model
    assert ev.payload["prompt_version"] == GEMINI_CURATION_SPEC.version
    assert ev.payload["raw_relevance"] == "signal"


@pytest.mark.asyncio
async def test_append_noise_records_curated_noise(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """noise 経路で code=curated_noise が記録される。"""
    article = await _make_article(db_session, sample_source)
    async with session_factory() as session:
        await CurationAuditRepository(session).append_noise(
            ready=_ready(article),
            envelope=_noise_envelope(),
            code="curated_noise",
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.outcome_code == "curated_noise"
    assert ev.category == "success"
    assert ev.code == "curated_noise"
    # PR1-a: raw_relevance は envelope.raw_relevance ("noise") から焼かれる
    assert ev.payload["raw_relevance"] == "noise"


# ---------------------------------------------------------------------------
# DROP 経路 — append_drop_article
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_drop_article_records_failure_with_drop_category(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """drop 経路で category=non_retryable_drop_article / code=exc.code が記録。

    本番の failure_handling は AIProviderError を ACL で Stage 3 marker に
    詰め替えてから本 method を呼ぶため、テストも同じ流れを再現する。
    """
    article = await _make_article(db_session, sample_source)
    article_id = article.id
    raw_exc = AIProviderOutputBlockedError()
    try:
        raise map_provider_to_curation(raw_exc) from raw_exc
    except Exception as wrapped:  # noqa: BLE001
        exc = wrapped
    curator = _curator_mock()

    async with session_factory() as session:
        await CurationAuditRepository(session).append_drop_article(
            article_id=article_id,
            original_content=article.original_content,
            code=exc.code,
            exc=exc,
            curator=curator,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article_id)
    assert ev.event_type == "failed"
    assert ev.outcome_code == "ai_error_output_blocked"
    assert ev.category == "non_retryable_drop_article"
    assert ev.code == "ai_error_output_blocked"
    assert ev.error_class is not None
    assert ev.error_class.endswith(".CurationTerminalDropError")
    assert ev.payload["error_message"] is not None
    assert ev.payload["error_chain"]
    # __cause__ chain に元 provider error も保持される
    assert ev.payload["error_chain"][0].endswith(".CurationTerminalDropError")
    assert any(
        s.endswith(".AIProviderOutputBlockedError") for s in ev.payload["error_chain"]
    )
    # PR2: 失敗 audit の ai_model / prompt_version は extractor 経由
    # (Gemini ClassVar hardcode を消した)
    assert ev.payload["ai_model"] == "test-extract-model"
    assert ev.payload["prompt_version"] == "test-extract-prompt-v1"


# ---------------------------------------------------------------------------
# 救済断念経路 — append_backfill_curation_aged_out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_backfill_curation_aged_out_records_rejected_with_aged_code(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """年齢削除の監査は drop と別 stage/event_type/category/code で記録される。

    意図的な組合せ: stage=backfill_curate (curation 救済の保守動作) +
    payload.kind=curation。content 拒否の drop (stage=curation /
    category=non_retryable_drop_article) とは全軸が異なる。
    """
    from app.audit.stages.curation import (
        BACKFILL_CURATION_AGED_OUT_CODE,
    )

    article = await _make_article(db_session, sample_source)
    async with session_factory() as session:
        await CurationAuditRepository(session).append_backfill_curation_aged_out(
            article_id=article.id
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.stage == "backfill_curate"
    assert ev.event_type == "rejected"
    assert ev.code == BACKFILL_CURATION_AGED_OUT_CODE
    assert ev.outcome_code == BACKFILL_CURATION_AGED_OUT_CODE
    # 年齢削除は curation 分類ではないので category は NULL
    assert ev.category is None
    # payload は curation variant (FK 切断耐性のため source_name を保持)
    assert ev.payload["kind"] == "curation"
    assert ev.payload["source_name"] == str(sample_source.name)


# ---------------------------------------------------------------------------
# 失敗経路 — append_failure (4 marker dispatch)
# ---------------------------------------------------------------------------


def _wrap(raw: BaseException) -> BaseException:
    """ACL で詰め替え + ``__cause__`` を保持する helper。"""
    try:
        raise map_provider_to_curation(raw) from raw  # type: ignore[arg-type]
    except BaseException as wrapped:  # noqa: BLE001
        return wrapped


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc_factory", "expected_category", "expected_code"),
    [
        (
            lambda: _wrap(AIProviderInputRejectedError()),
            "non_retryable_drop_article",
            "ai_error_input_rejected",
        ),
        (
            lambda: _wrap(AIProviderConfigurationError()),
            "non_retryable_keep_article",
            "ai_error_configuration",
        ),
        (
            lambda: _wrap(AIProviderNetworkError()),
            "retryable",
            "ai_error_network",
        ),
        (
            lambda: CurationResponseInvalidError(),
            "retryable",
            "extraction_response_invalid",
        ),
        (lambda: RuntimeError("surprise"), "unknown", "unexpected_error"),
        # 外部 DB 例外は classify_db_error adapter で意味ラベルに分類される
        # (SQLAlchemy が振る .code=gkpj 等を拾わない)。Stage 3 の KEEP は
        # NON_RETRYABLE_KEEP_ARTICLE。
        (
            lambda: OperationalError("SELECT 1", {}, Exception("conn reset")),
            "retryable",
            "db_runtime_error",
        ),
        (
            lambda: IntegrityError("INSERT", {}, Exception("unique violation")),
            "non_retryable_keep_article",
            "db_constraint_error",
        ),
        (
            lambda: ProgrammingError("SELECT bad", {}, Exception("no such column")),
            "non_retryable_keep_article",
            "db_query_or_schema_error",
        ),
        (
            lambda: InvalidRequestError("detached instance"),
            "unknown",
            "db_unknown_error",
        ),
    ],
)
async def test_append_failure_dispatches_category_and_code_from_exc(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    exc_factory: object,
    expected_category: str,
    expected_code: str,
) -> None:
    """append_failure は exc 型から category/code を自動導出する。"""
    article = await _make_article(db_session, sample_source)
    exc = exc_factory()  # type: ignore[operator]
    curator = _curator_mock()

    async with session_factory() as session:
        await CurationAuditRepository(session).append_failure(
            ready=_ready(article),
            exc=exc,
            attempt=2,
            curator=curator,
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.event_type == "failed"
    assert ev.category == expected_category
    assert ev.code == expected_code
    assert ev.outcome_code == expected_code  # Phase A: outcome_code = code
    assert ev.attempt == 2
    assert ev.error_class is not None
    assert ev.error_class.endswith(f".{type(exc).__name__}")
    # PR2: 失敗 audit の ai_model / prompt_version は extractor 経由
    # (Gemini ClassVar hardcode を消した)
    assert ev.payload["ai_model"] == "test-extract-model"
    assert ev.payload["prompt_version"] == "test-extract-prompt-v1"


# ---------------------------------------------------------------------------
# tx 境界 — repository は commit しない
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repository_does_not_commit(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """repository が caller commit を奪わないことを確認する。"""
    article = await _make_article(db_session, sample_source)

    async with session_factory() as session:
        await CurationAuditRepository(session).append_signal(
            ready=_ready(article),
            envelope=_signal_envelope(),
            code="curated_signal",
        )
        # 意図的に commit しない (rollback で消える)

    rows = (
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.article_id == article.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 0  # 未 commit のため永続化されていない
