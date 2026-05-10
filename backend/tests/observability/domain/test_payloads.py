"""``ClassificationPayload`` の ``kind`` discriminator が PR4 で 'assessment' に
変わったことを pin する test。

PR4: class 名は据置 (``ClassificationPayload``) だが ``kind`` Literal 値だけ
"classification" → "assessment" に変えた一時状態を test で固定する。

PR5 で ``AssessmentPayload`` に置換 / ``ClassificationPayload`` 削除する際、
本 file も一緒に削除する (PR5 plan の checklist に入れる)。
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from app.observability.domain.payloads import (
    ClassificationPayload,
    PipelineEventPayload,
)


def test_classification_payload_kind_is_assessment_post_pr4() -> None:
    """PR4: class 名は据置だが kind 値だけ 'assessment' に変えた一時状態を固定。

    Literal 値 / default 値の両方が "assessment" に置換済みであることを保証する。
    PR5 で AssessmentPayload に置換する際、本 test も一緒に削除する。
    """
    payload = ClassificationPayload()
    assert payload.kind == "assessment"
    field_default = ClassificationPayload.model_fields["kind"].default
    assert field_default == "assessment"


def test_classification_payload_parses_via_assessment_discriminator() -> None:
    """discriminated union 経由で kind="assessment" の dict が
    ClassificationPayload に解決する。

    PR4 deploy 後、新 row は ``kind="assessment"`` で書かれる。Pydantic v2
    の discriminated union がこの値を ``ClassificationPayload`` member に
    dispatch することを担保する。
    """
    adapter: TypeAdapter[PipelineEventPayload] = TypeAdapter(PipelineEventPayload)
    parsed = adapter.validate_python({"kind": "assessment"})
    assert isinstance(parsed, ClassificationPayload)


def test_classification_payload_rejects_legacy_classification_kind() -> None:
    """PR4 後は kind="classification" を読まない (新 schema 一本化の保証)。

    deploy 戦略が stop-the-world で、deploy 完了時点で既存 row の payload も
    migration により ``kind="assessment"`` に書き換わっているため、PR4 以降の
    Pydantic discriminated union は ``"classification"`` discriminator を
    受理してはならない。
    """
    adapter: TypeAdapter[PipelineEventPayload] = TypeAdapter(PipelineEventPayload)
    # discriminator 値違反は Pydantic v2 で ValidationError を raise する。
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "classification"})
