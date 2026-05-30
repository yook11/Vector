"""``SourceAcquisitionAuditRepository`` の origin specifics payload 写像の単体テスト。

統合 marker ``AcquisitionReadError`` の origin を見て、read origin
(``UnreadableResponseError``) は ``read_*`` 列へ、fetch origin (``ExternalFetchError``)
は ``http_status`` / ``fetch_*`` 列へ写す純粋写像を、DB を経由せず直接固定する。
該当しない側が None に保たれること (read / fetch がそれぞれ自分の specifics だけを
焼く) を併せて固定する非空虚 oracle。
"""

from __future__ import annotations

from app.audit.stages.acquisition import _origin_payload_fields
from app.collection.article_acquisition.errors import AcquisitionReadError
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
    UnreadableResponseReason,
)
from app.collection.external_fetch_errors import (
    FetchGatewayError,
    FetchOriginServerError,
)


def test_read_failure_fields_carry_origin_specifics() -> None:
    """read marker は origin の format / field / position を read_* 列へ写し、
    fetch 列は None に保つ。"""
    origin = UnreadableResponseError(
        reason=UnreadableResponseReason.UNEXPECTED_FIELD_SHAPE,
        response_format="json",
        field="items",
        parser_position="3:7",
    )
    marker = AcquisitionReadError(origin=origin)

    assert _origin_payload_fields(marker) == {
        "read_format": "json",
        "read_field": "items",
        "read_parser_position": "3:7",
        "http_status": None,
        "fetch_reason": None,
        "fetch_retry_after_seconds": None,
    }


def test_fetch_failure_fields_carry_status_only_when_origin_lacks_reason() -> None:
    """status_code のみ持つ fetch origin (gateway 502) → http_status だけ載り、
    reason / retry_after と read_* は None。"""
    marker = AcquisitionReadError(origin=FetchGatewayError(status_code=502))

    assert _origin_payload_fields(marker) == {
        "read_format": None,
        "read_field": None,
        "read_parser_position": None,
        "http_status": 502,
        "fetch_reason": None,
        "fetch_retry_after_seconds": None,
    }


def test_fetch_failure_fields_carry_reason_and_retry_after() -> None:
    """reason + retry_after を持つ fetch origin (503) → fetch 3 列すべてに値が載る。"""
    marker = AcquisitionReadError(
        origin=FetchOriginServerError(
            status_code=503,
            reason="service_unavailable",
            retry_after_seconds=30.0,
        )
    )

    assert _origin_payload_fields(marker) == {
        "read_format": None,
        "read_field": None,
        "read_parser_position": None,
        "http_status": 503,
        "fetch_reason": "service_unavailable",
        "fetch_retry_after_seconds": 30.0,
    }


def test_non_marker_exception_has_no_origin_fields() -> None:
    """origin を持たない想定外例外は全 origin 列を None に保つ。"""
    assert _origin_payload_fields(RuntimeError("boom")) == {
        "read_format": None,
        "read_field": None,
        "read_parser_position": None,
        "http_status": None,
        "fetch_reason": None,
        "fetch_retry_after_seconds": None,
    }
