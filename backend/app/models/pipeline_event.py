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
            # migration z6_briefing_audit_setup と完全に揃える
            # (metadata.create_all 経由のテスト DB が古い CHECK を持たないように)。
            # 10 値 (article-bound 9 stage + briefing)。
            "stage IN ("
            "'dispatch','acquisition','completion',"
            "'curation','assessment','embedding',"
            "'backfill_curate','backfill_assess','backfill_embed',"
            "'briefing'"
            ")",
            name="ck_pipeline_events_stage",
        ),
        CheckConstraint(
            "event_type IN ('succeeded','skipped','rejected','failed')",
            name="ck_pipeline_events_event_type",
        ),
        CheckConstraint(
            "retryability IS NULL OR retryability IN "
            "('retryable','non_retryable','unknown')",
            name="ck_pipeline_events_retryability",
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
    retryability: Mapped[str | None] = mapped_column(String(20), nullable=True)
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
    error_class: Mapped[str | None] = mapped_column(String(160), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
