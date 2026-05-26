"""監査 bounded context — pipeline_events 監査基盤の実装。

詳細は ``docs/observability/pipeline-events-design.md`` 参照。

依存方向: ``collection`` / ``analysis`` / ``insights`` → ``audit`` (片方向)。
per-stage semantic API は ``app.audit.stages`` に集約。
"""

from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import (
    AcquisitionPayload,
    AssessmentPayload,
    BasePipelineEventPayload,
    BriefingPayload,
    CompletionPayload,
    CurationPayload,
    DispatchPayload,
    EmbeddingPayload,
    PipelineEventPayload,
)
from app.audit.repository import PipelineEventRepository

__all__ = [
    "AcquisitionPayload",
    "AssessmentPayload",
    "BasePipelineEventPayload",
    "BriefingPayload",
    "CompletionPayload",
    "CurationPayload",
    "DispatchPayload",
    "EmbeddingPayload",
    "EventType",
    "PipelineEventPayload",
    "PipelineEventRepository",
    "Stage",
]
