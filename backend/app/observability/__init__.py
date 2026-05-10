"""観測 bounded context — pipeline_events 監査基盤の実装。

詳細は ``docs/observability/pipeline-events-design.md`` 参照。

依存方向: ``collection`` / ``analysis`` → ``observability`` (片方向)。
"""

from app.observability.domain.event import EventType, Stage
from app.observability.domain.payloads import (
    AssessmentPayload,
    BasePipelineEventPayload,
    ContentFetchPayload,
    DispatchPayload,
    EmbeddingPayload,
    ExtractionPayload,
    PipelineEventPayload,
    SourceFetchPayload,
)
from app.observability.prompt_versions import compute_call_signature
from app.observability.recording import _record_failure_event
from app.observability.repository import PipelineEventRepository

__all__ = [
    "AssessmentPayload",
    "BasePipelineEventPayload",
    "ContentFetchPayload",
    "DispatchPayload",
    "EmbeddingPayload",
    "EventType",
    "ExtractionPayload",
    "PipelineEventPayload",
    "PipelineEventRepository",
    "SourceFetchPayload",
    "Stage",
    "_record_failure_event",
    "compute_call_signature",
]
