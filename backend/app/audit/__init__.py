"""監査 bounded context — pipeline_events 監査基盤の実装。

詳細は ``docs/observability/pipeline-events-design.md`` 参照。

依存方向: ``collection`` / ``analysis`` → ``audit`` (片方向)。
"""

from app.audit.domain.event import EventType, Stage
from app.audit.domain.payloads import (
    AcquisitionPayload,
    AssessmentPayload,
    BasePipelineEventPayload,
    CompletionPayload,
    CurationPayload,
    DispatchPayload,
    EmbeddingPayload,
    PipelineEventPayload,
)
from app.audit.recording import _record_failure_event
from app.audit.repository import PipelineEventRepository

__all__ = [
    "AssessmentPayload",
    "BasePipelineEventPayload",
    "CompletionPayload",
    "CurationPayload",
    "DispatchPayload",
    "EmbeddingPayload",
    "EventType",
    "PipelineEventPayload",
    "PipelineEventRepository",
    "AcquisitionPayload",
    "Stage",
    "_record_failure_event",
]
