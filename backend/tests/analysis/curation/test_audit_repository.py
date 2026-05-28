"""``CurationAuditRepository`` の semantic method 単独テスト (PR3.5-c)。

audit row の shape SSoT が repository に集約されたことを検証する:

- ``append_signal`` / ``append_noise`` で
  ``outcome_code`` と成功 payload が記録される
- ``append_drop_article`` で
  Stage 3 marker の ``code`` 由来の ``outcome_code`` と failure attrs が記録
- ``append_failure`` で **Stage 3 marker 型による dispatch** が動作:
  - ``CurationTerminalDropError`` → ``retryability=non_retryable`` / ``drop_article``
  - ``CurationTerminalKeepError`` → ``retryability=non_retryable``
  - ``CurationRecoverableError`` → ``retryability=retryable``
  - 想定外 ``RuntimeError`` → ``retryability=unknown`` /
    ``outcome_code=unexpected_error``
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
from app.analysis.curation.domain.ready import (
    CurationReadyBuildBlocked,
    CurationReadyBuildBlockedCode,
    ReadyForCuration,
)
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
    prompt_version / rate_limit_policy) に置き換わったため、property 属性として
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


async def _fetch_by_outcome(
    db_session: AsyncSession, outcome_code: str
) -> PipelineEvent:
    rows = (
        (
            await db_session.execute(
                select(PipelineEvent).where(PipelineEvent.outcome_code == outcome_code)
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
async def test_append_ready_build_blocked_records_missing_article_rejected(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ready build blocked は rejected として trigger article id を payload に残す。"""
    async with session_factory() as session:
        await CurationAuditRepository(session).append_ready_build_blocked(
            blocked=CurationReadyBuildBlocked(
                target_article_id=999,
                code=CurationReadyBuildBlockedCode.ARTICLE_MISSING,
            )
        )
        await session.commit()

    ev = await _fetch_by_outcome(
        db_session, "curation_ready_build_blocked_article_missing"
    )
    assert ev.event_type == "rejected"
    assert ev.article_id is None
    assert ev.payload["target_article_id"] == 999


@pytest.mark.asyncio
async def test_append_ready_build_blocked_records_content_too_large(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """content too large は article FK と入力サイズ snapshot を残す。"""
    article = await _make_article(db_session, sample_source)
    async with session_factory() as session:
        await CurationAuditRepository(session).append_ready_build_blocked(
            blocked=CurationReadyBuildBlocked(
                target_article_id=article.id,
                code=CurationReadyBuildBlockedCode.CONTENT_TOO_LARGE,
                content_length=200_001,
                max_content_length=200_000,
                source_name=str(sample_source.name),
            )
        )
        await session.commit()

    ev = await _fetch_by_outcome(
        db_session, "curation_ready_build_blocked_content_too_large"
    )
    assert ev.event_type == "rejected"
    assert ev.article_id == article.id
    assert ev.payload["source_name"] == str(sample_source.name)
    assert ev.payload["input_content_length"] == 200_001
    assert ev.payload["max_content_length"] == 200_000


@pytest.mark.asyncio
async def test_append_ready_build_failed_records_unknown_failure(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ready build failed は failed / unknown retryability で trigger id を残す。"""
    exc = RuntimeError("ready build exploded")
    async with session_factory() as session:
        await CurationAuditRepository(session).append_ready_build_failed(
            target_article_id=123,
            exc=exc,
        )
        await session.commit()

    ev = await _fetch_by_outcome(
        db_session, "curation_ready_build_failed_unexpected_error"
    )
    assert ev.event_type == "failed"
    assert ev.retryability == "unknown"
    assert ev.error_class == "builtins.RuntimeError"
    assert ev.payload["failure_kind"] == "unexpected_error"
    assert ev.payload["target_article_id"] == 123


@pytest.mark.asyncio
async def test_append_signal_records_success_with_code(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
) -> None:
    """signal 経路で succeeded / outcome_code=curated_signal が記録される。"""
    article = await _make_article(db_session, sample_source)
    async with session_factory() as session:
        await CurationAuditRepository(session).append_signal(
            ready=_ready(article),
            envelope=_signal_envelope(),
            code="curated_signal",
            input_content_length=123,
            input_content_head="CALLER_PRECOMPUTED_HEAD",
            input_content_hash="CALLER_HASH_16XX",
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.event_type == "succeeded"
    assert ev.outcome_code == "curated_signal"
    assert ev.retryability is None
    assert ev.payload["ai_raw_response"]
    assert ev.payload["source_name"] == str(sample_source.name)
    # caller pre-compute 値がそのまま payload に焼かれる (repository は計算しない)
    assert ev.payload["input_content_length"] == 123
    assert ev.payload["input_content_head"] == "CALLER_PRECOMPUTED_HEAD"
    assert ev.payload["input_content_hash"] == "CALLER_HASH_16XX"
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
    """noise 経路で outcome_code=curated_noise が記録される。"""
    article = await _make_article(db_session, sample_source)
    async with session_factory() as session:
        await CurationAuditRepository(session).append_noise(
            ready=_ready(article),
            envelope=_noise_envelope(),
            code="curated_noise",
            input_content_length=456,
            input_content_head="NOISE_PRECOMPUTED_HEAD",
            input_content_hash="NOISE_HASH_16XYZ",
        )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.outcome_code == "curated_noise"
    assert ev.retryability is None
    # caller pre-compute 値がそのまま payload に焼かれる
    assert ev.payload["input_content_length"] == 456
    assert ev.payload["input_content_head"] == "NOISE_PRECOMPUTED_HEAD"
    assert ev.payload["input_content_hash"] == "NOISE_HASH_16XYZ"
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
    """drop 経路で failure attrs と outcome_code=exc.code が記録。

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
            code=exc.code,
            exc=exc,
            curator=curator,
            input_content_length=789,
            input_content_head="DROP_PRECOMPUTED_HEAD",
            input_content_hash="DROP_HASH_16ABCDEF",
        )
        await session.commit()

    ev = await _fetch_one(db_session, article_id)
    assert ev.event_type == "failed"
    assert ev.outcome_code == "ai_error_output_blocked"
    assert ev.retryability == "non_retryable"
    assert ev.error_class is not None
    assert ev.error_class.endswith(".CurationTerminalDropError")
    assert ev.payload["failure_kind"] == "terminal_drop"
    assert ev.payload["failure_action"] == "drop_article"
    assert ev.payload["error_message"] is not None
    assert ev.payload["error_chain"]
    # __cause__ chain に元 provider error も保持される
    assert ev.payload["error_chain"][0].endswith(".CurationTerminalDropError")
    assert any(
        s.endswith(".AIProviderOutputBlockedError") for s in ev.payload["error_chain"]
    )
    # caller pre-compute 値がそのまま payload に焼かれる (drop path も同形)
    assert ev.payload["input_content_length"] == 789
    assert ev.payload["input_content_head"] == "DROP_PRECOMPUTED_HEAD"
    assert ev.payload["input_content_hash"] == "DROP_HASH_16ABCDEF"
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
    """年齢削除の監査は drop と別 stage/event_type/outcome_code で記録される。

    意図的な組合せ: stage=backfill_curate (curation 救済の保守動作) +
    payload.kind=curation。content 拒否の drop (stage=curation / failed /
    outcome_code=ai_error_*) とは全軸が異なる。
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
    assert ev.outcome_code == BACKFILL_CURATION_AGED_OUT_CODE
    assert ev.retryability is None
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
    (
        "exc_factory",
        "expected_outcome_code",
        "expected_retryability",
        "expected_failure_kind",
        "expected_failure_action",
    ),
    [
        (
            lambda: _wrap(AIProviderInputRejectedError()),
            "ai_error_input_rejected",
            "non_retryable",
            "terminal_drop",
            "drop_article",
        ),
        (
            lambda: _wrap(AIProviderConfigurationError()),
            "ai_error_configuration",
            "non_retryable",
            "terminal_keep",
            None,
        ),
        (
            lambda: _wrap(AIProviderNetworkError()),
            "ai_error_network",
            "retryable",
            "recoverable",
            None,
        ),
        (
            lambda: CurationResponseInvalidError(),
            "extraction_response_invalid",
            "retryable",
            "recoverable",
            None,
        ),
        (
            lambda: RuntimeError("surprise"),
            "unexpected_error",
            "unknown",
            "unknown",
            None,
        ),
        # 外部 DB 例外は classify_db_error adapter で意味ラベルに分類される
        # (SQLAlchemy が振る .code=gkpj 等を拾わない)。
        (
            lambda: OperationalError("SELECT 1", {}, Exception("conn reset")),
            "db_runtime_error",
            "retryable",
            "db_runtime",
            None,
        ),
        (
            lambda: IntegrityError("INSERT", {}, Exception("unique violation")),
            "db_constraint_error",
            "non_retryable",
            "db_constraint",
            None,
        ),
        (
            lambda: ProgrammingError("SELECT bad", {}, Exception("no such column")),
            "db_query_or_schema_error",
            "non_retryable",
            "db_query_or_schema",
            None,
        ),
        (
            lambda: InvalidRequestError("detached instance"),
            "db_unknown_error",
            "unknown",
            "db_unknown",
            None,
        ),
    ],
)
async def test_append_failure_dispatches_failure_projection_from_exc(
    db_session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    sample_source: NewsSource,
    exc_factory: object,
    expected_outcome_code: str,
    expected_retryability: str,
    expected_failure_kind: str,
    expected_failure_action: str | None,
) -> None:
    """append_failure は exc 型から failure projection を自動導出する。"""
    article = await _make_article(db_session, sample_source)
    exc = exc_factory()  # type: ignore[operator]
    curator = _curator_mock()

    async with session_factory() as session:
        repo = CurationAuditRepository(session)
        if isinstance(exc, RuntimeError):
            await repo.append_unexpected_failure(
                ready=_ready(article),
                exc=exc,
                curator=curator,
                input_content_length=42,
                input_content_head="FAIL_PRECOMPUTED_HEAD",
                input_content_hash="FAIL_HASH_16AAAAA",
            )
        else:
            await repo.append_failure(
                ready=_ready(article),
                exc=exc,
                curator=curator,
                input_content_length=42,
                input_content_head="FAIL_PRECOMPUTED_HEAD",
                input_content_hash="FAIL_HASH_16AAAAA",
            )
        await session.commit()

    ev = await _fetch_one(db_session, article.id)
    assert ev.event_type == "failed"
    assert ev.outcome_code == expected_outcome_code
    assert ev.retryability == expected_retryability
    assert ev.error_class is not None
    assert ev.error_class.endswith(f".{type(exc).__name__}")
    assert ev.payload["failure_kind"] == expected_failure_kind
    assert ev.payload["failure_action"] == expected_failure_action
    # caller pre-compute 値がそのまま payload に焼かれる (failure path も同形)
    assert ev.payload["input_content_length"] == 42
    assert ev.payload["input_content_head"] == "FAIL_PRECOMPUTED_HEAD"
    assert ev.payload["input_content_hash"] == "FAIL_HASH_16AAAAA"
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
            input_content_length=1,
            input_content_head="x",
            input_content_hash="0000000000000000",
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
