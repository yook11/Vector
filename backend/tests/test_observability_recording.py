"""``app.observability.recording`` の単体テスト。

- ``_extract_error_chain`` の cause/context 走査と循環防止
- ``build_failure_payload`` が Stage に対応する Payload を返す
- ``_record_failure_event`` 正常系: 新 session で 1 行 INSERT
- ``_record_failure_event`` DB 失敗: structlog で fallback ログを残す
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.models.pipeline_event import PipelineEvent
from app.observability.domain.event import Stage
from app.observability.domain.payloads import (
    ClassificationPayload,
    ContentFetchPayload,
    EmbeddingPayload,
    ExtractionPayload,
    SourceFetchPayload,
)
from app.observability.recording import (
    _extract_error_chain,
    _record_failure_event,
    build_failure_payload,
)


def test_error_chain_walks_cause() -> None:
    try:
        try:
            raise ValueError("inner")
        except ValueError as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError as e:
        chain = _extract_error_chain(e)

    assert chain[0].endswith(".RuntimeError")
    assert chain[1].endswith(".ValueError")


def test_error_chain_walks_context_when_no_cause() -> None:
    """`raise X` 中に別 raise (no `from`) は __context__ で辿れる。"""
    try:
        try:
            raise ValueError("inner")
        except ValueError:
            raise RuntimeError("outer")  # noqa: B904
    except RuntimeError as e:
        chain = _extract_error_chain(e)

    assert chain[0].endswith(".RuntimeError")
    assert chain[1].endswith(".ValueError")


def test_error_chain_handles_cycle() -> None:
    """循環 (self-cause) があっても無限ループしない。"""
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    chain = _extract_error_chain(a)
    assert len(chain) <= 8  # _MAX_CHAIN_DEPTH


def test_build_failure_payload_returns_correct_subclass() -> None:
    exc = ValueError("boom")
    payload = build_failure_payload(Stage.EXTRACTION, exc)
    assert isinstance(payload, ExtractionPayload)
    assert payload.error_message == "boom"
    assert payload.error_chain is not None
    assert payload.error_chain[0].endswith(".ValueError")


def test_build_failure_payload_for_each_stage_variant() -> None:
    exc = RuntimeError("x")
    cases: list[tuple[Stage, type]] = [
        (Stage.SOURCE_FETCH, SourceFetchPayload),
        (Stage.CONTENT_FETCH, ContentFetchPayload),
        # PR4: 旧 Stage.CLASSIFICATION → Stage.ASSESSMENT。value は据置で
        # ClassificationPayload (PR5 で AssessmentPayload に置換予定)。
        (Stage.ASSESSMENT, ClassificationPayload),
        (Stage.EMBEDDING, EmbeddingPayload),
    ]
    for stage, expected_cls in cases:
        payload = build_failure_payload(stage, exc)
        assert isinstance(payload, expected_cls)


def test_build_failure_payload_redacts_secrets_in_error_message() -> None:
    """red-team chain γ-2: secret prefix が永続化前に伏字化される。"""
    exc = RuntimeError(
        "Authorization: Bearer "
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.SflKxwRJSMeKKF2QT4abc failed"
    )
    payload = build_failure_payload(Stage.EXTRACTION, exc)

    assert payload.error_message is not None
    assert "SflKxwRJSMeKKF2QT4abc" not in payload.error_message
    assert "eyJhbGciOiJIUzI1NiJ9" not in payload.error_message
    assert "***" in payload.error_message


def test_build_failure_payload_preserves_normal_message() -> None:
    """secret なしの普通 exception は元 message が変わらない (可読性保持)。"""
    exc = RuntimeError("Connection refused on host db.internal port 5432")
    payload = build_failure_payload(Stage.EXTRACTION, exc)
    assert payload.error_message == "Connection refused on host db.internal port 5432"


def test_build_failure_payload_truncation_after_redact() -> None:
    """redact pass で長さが変わっても [:_ERR_MSG_LIMIT] 切詰めが正しく動作する。"""
    secret = "AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6Q"
    long_filler = "x" * 5000
    exc = RuntimeError(f"prefix {secret} {long_filler}")
    payload = build_failure_payload(Stage.EXTRACTION, exc)

    assert payload.error_message is not None
    assert len(payload.error_message) <= 2000
    assert "AIza***" in payload.error_message
    assert secret not in payload.error_message


@pytest.mark.asyncio
async def test_record_failure_event_inserts_row(
    session_factory: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    exc = RuntimeError("permanent fetch boom")
    await _record_failure_event(
        session_factory=session_factory,
        stage=Stage.SOURCE_FETCH,
        outcome_code="permanent_fetch_error",
        exc=exc,
        attempt=2,
        duration_ms=123,
    )

    rows = (await db_session.execute(select(PipelineEvent))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == "failed"
    assert row.outcome_code == "permanent_fetch_error"
    assert row.attempt == 2
    assert row.duration_ms == 123
    assert row.error_class.endswith(".RuntimeError")  # type: ignore[union-attr]
    assert row.payload["error_message"] == "permanent fetch boom"


@pytest.mark.asyncio
async def test_record_failure_event_falls_back_to_log_on_db_error() -> None:
    """session_factory が常に raise する場合、第 2 防御 (log) で観測されること。"""

    class _BoomFactory:
        def __call__(self) -> Any:  # session_factory() で呼ばれる
            raise RuntimeError("db down")

    business_exc = ValueError("net timeout")
    with capture_logs() as cap:
        await _record_failure_event(
            session_factory=_BoomFactory(),  # type: ignore[arg-type]
            stage=Stage.SOURCE_FETCH,
            outcome_code="temporary_fetch_error_exhausted",
            exc=business_exc,
            attempt=3,
            duration_ms=999,
        )

    drops = [
        e for e in cap if e.get("event") == "pipeline_event_record_failure_dropped"
    ]
    assert drops, "fallback ログが emit されていない"
    drop = drops[-1]
    assert drop["outcome_code"] == "temporary_fetch_error_exhausted"
    assert drop["business_error_class"].endswith(".ValueError")
    assert drop["business_error_message"] == "net timeout"
    assert drop["audit_error_class"].endswith(".RuntimeError")
