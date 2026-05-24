"""パイプライン監査イベント (append-only)。

詳細は ``docs/observability/pipeline-events-design.md`` 参照。

- 全 9 Stage × 4 EventType を 1 行 = 1 イベントで表現
- 業務 tx と同一トランザクション (成功/skip パス) または別 session (例外パス) で書込
- ``payload`` は Pydantic Discriminated Union (``app/audit/domain/payloads.py``)
- ``Base.metadata`` は ``SQLModel.metadata`` と共有 (``app/models/base.py``)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PipelineEvent(Base):
    """append-only パイプライン監査イベント。"""

    __tablename__ = "pipeline_events"
    __table_args__ = (
        CheckConstraint(
            # migration z1_curation_completion_rename と完全に揃える
            # (metadata.create_all 経由のテスト DB が古い CHECK を持たないように)。
            "stage IN ("
            "'dispatch','acquisition','completion',"
            "'curation','assessment','embedding',"
            "'backfill_extract','backfill_assess','backfill_embed'"
            ")",
            name="ck_pipeline_events_stage",
        ),
        CheckConstraint(
            "event_type IN ('succeeded','skipped','rejected','failed')",
            name="ck_pipeline_events_event_type",
        ),
        CheckConstraint(
            # PR4: 'non_retryable_keep_extraction' を追加。assessment が回復不能でも
            # extraction 結果は保存維持する用途 (AssessmentTerminalSkipError dispatch)。
            "category IS NULL OR category IN ("
            "'success','idempotent_skip','retryable',"
            "'non_retryable_drop_article','non_retryable_keep_article',"
            "'non_retryable_keep_extraction',"
            "'unknown'"
            ")",
            name="ck_pipeline_events_category",
        ),
        CheckConstraint(
            "attempt >= 1",
            name="ck_pipeline_events_attempt_positive",
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_pipeline_events_duration_nonneg",
        ),
        Index(
            "ix_pipeline_events_stage_outcome",
            "stage",
            "event_type",
            "outcome_code",
            "occurred_at",
        ),
        Index(
            "ix_pipeline_events_source_id",
            "source_id",
            "occurred_at",
            postgresql_where=text("source_id IS NOT NULL"),
        ),
        Index(
            "ix_pipeline_events_article_id",
            "article_id",
            "occurred_at",
            postgresql_where=text("article_id IS NOT NULL"),
        ),
        Index(
            "ix_pipeline_events_failed",
            "occurred_at",
            postgresql_where=text("event_type = 'failed'"),
        ),
        Index(
            "ix_pipeline_events_payload_gin",
            "payload",
            postgresql_using="gin",
            postgresql_ops={"payload": "jsonb_path_ops"},
        ),
        # BRIN(occurred_at) は migration のみ作成 (テスト DB は scan で機能上問題なし)
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    outcome_code: Mapped[str] = mapped_column(String(60), nullable=False)
    category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    source_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("news_sources.id", ondelete="SET NULL"),
        nullable=True,
    )
    article_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("articles.id", ondelete="SET NULL"),
        nullable=True,
    )
    attempt: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1")
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(160), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
