"""``SourceAcquisitionAuditRepository`` の read 失敗 payload 写像の単体テスト。

read marker の origin (``UnreadableResponseError``) が持つ specifics を
``AcquisitionPayload`` の ``read_*`` 列へ写す純粋写像を、DB を経由せず直接固定する。
接続失敗 / 想定外例外では read_* が None に保たれ payload に影響しないことを併せて
固定する (read 経路だけが read_* を焼く非空虚 oracle)。
"""

from __future__ import annotations

from app.audit.stages.acquisition import _read_failure_payload_fields
from app.collection.article_acquisition.errors import (
    AcquisitionExternalFetchError,
    AcquisitionUnreadableResponseError,
)
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
    UnreadableResponseReason,
)
from app.collection.external_fetch_errors import FetchGatewayError


def test_read_failure_fields_carry_origin_specifics() -> None:
    """read marker は origin の format / field / position を payload 列へ写す。"""
    origin = UnreadableResponseError(
        reason=UnreadableResponseReason.UNEXPECTED_FIELD_SHAPE,
        response_format="json",
        field="items",
        parser_position="3:7",
    )
    marker = AcquisitionUnreadableResponseError(origin_error=origin)

    assert _read_failure_payload_fields(marker) == {
        "read_format": "json",
        "read_field": "items",
        "read_parser_position": "3:7",
    }


def test_fetch_failure_has_no_read_fields() -> None:
    """接続失敗 (read でない) は read_* を全て None に保つ (payload 無影響)。"""
    marker = AcquisitionExternalFetchError(
        origin_error=FetchGatewayError(status_code=502)
    )

    assert _read_failure_payload_fields(marker) == {
        "read_format": None,
        "read_field": None,
        "read_parser_position": None,
    }


def test_non_marker_exception_has_no_read_fields() -> None:
    """origin_error を持たない想定外例外も read_* を None に保つ。"""
    assert _read_failure_payload_fields(RuntimeError("boom")) == {
        "read_format": None,
        "read_field": None,
        "read_parser_position": None,
    }
