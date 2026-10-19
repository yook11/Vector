"""監査 bounded context — pipeline_events 監査基盤の実装。

詳細は ``docs/observability/pipeline-events-design.md`` 参照。

per-stage semantic API は ``app.audit.stages`` に集約し、stage 固有の DTO /
payload snapshot 規律を受け取って監査行へ写像する。
"""

from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import (
    AssessmentPayload,
    BackfillPayload,
    BasePipelineEventPayload,
    BriefingPayload,
    CurationPayload,
    DispatchPayload,
    EmbeddingPayload,
    PipelineEventPayload,
)
from app.audit.repository import PipelineEventRepository

__all__ = [
    "AssessmentPayload",
    "BackfillPayload",
    "BasePipelineEventPayload",
    "BriefingPayload",
    "CurationPayload",
    "DispatchPayload",
    "EmbeddingPayload",
    "EventType",
    "PipelineEventPayload",
    "PipelineEventRepository",
    "Stage",
]
