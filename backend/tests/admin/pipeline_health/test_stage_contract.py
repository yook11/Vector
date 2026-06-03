"""pipeline health stage schema の契約テスト (DB 不要 unit)。"""

from __future__ import annotations

from app.admin.pipeline_health.schemas import PipelineStageHealth
from app.audit.domain.event import Stage


def test_pipeline_stage_health_uses_audit_stage_enum() -> None:
    """``stage`` schema は上流 ``Stage`` enum 全体から導出される。"""
    schema = PipelineStageHealth.model_json_schema()

    assert schema["properties"]["stage"] == {"$ref": "#/$defs/Stage"}
    assert schema["$defs"]["Stage"]["enum"] == [stage.value for stage in Stage]
