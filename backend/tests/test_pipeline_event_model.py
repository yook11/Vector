"""``PipelineEvent`` の語彙と DB 制約の契約テスト。

- StrEnum 値 set が CHECK 制約値と一致 (二重チェック)
- retryability 列の CHECK 制約 (実DB)
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pipeline_event import EventType, PipelineEvent, Stage


def test_stage_strenum_matches_check_constraint() -> None:
    """Stage StrEnum 値 set が ORM/migration の CHECK 制約値と一致。

    値追加時は両方の更新を要求する自然な fail-fast。11 値。
    """
    expected = {
        "dispatch",
        "acquisition",
        "completion",
        "curation",
        "assessment",
        "embedding",
        "backfill_curate",
        "backfill_assess",
        "backfill_embed",
        "briefing",
        "trend_discovery",
    }
    assert {s.value for s in Stage} == expected


def test_event_type_strenum_matches_check_constraint() -> None:
    expected = {"succeeded", "skipped", "rejected", "failed"}
    assert {e.value for e in EventType} == expected


def test_pipeline_event_top_level_columns_match_current_contract() -> None:
    """撤去済みの旧 3 列を戻さず、失敗詳細は payload に閉じることを固定する。"""
    expected = {
        "id",
        "occurred_at",
        "stage",
        "event_type",
        "outcome_code",
        "retryability",
        "source_id",
        "article_id",
        "error_class",
        "trace_id",
        "payload",
    }
    actual = set(PipelineEvent.__table__.columns.keys())
    assert actual == expected
    assert {"category", "code", "attempt"}.isdisjoint(actual)
    assert {"failure_kind", "failure_action", "attempt_count"}.isdisjoint(actual)


@pytest.mark.asyncio
async def test_retryability_check_constraint(db_session: AsyncSession) -> None:
    """retryability 列の CHECK 制約検証: 3 値 + NULL は OK、不正値は失敗。"""
    db_session.add(
        PipelineEvent(
            stage=Stage.DISPATCH.value,
            event_type=EventType.SKIPPED.value,
            outcome_code="test_null_retryability",
            payload={},
        )
    )
    # 許可値は ck_pipeline_events_retryability (app/models/pipeline_event.py)。
    for retryability in ("retryable", "non_retryable", "unknown"):
        db_session.add(
            PipelineEvent(
                stage=Stage.CURATION.value,
                event_type=EventType.FAILED.value,
                outcome_code=f"test_{retryability}",
                retryability=retryability,
                payload={},
            )
        )
    await db_session.commit()

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                "INSERT INTO pipeline_events "
                "(stage, event_type, outcome_code, retryability, payload) "
                "VALUES ('curation', 'failed', 'test', 'invalid_value', '{}')"
            )
        )
    await db_session.rollback()
