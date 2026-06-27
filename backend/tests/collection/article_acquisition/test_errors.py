"""Stage 1 article acquisition 例外の単体テスト。"""

from __future__ import annotations

import pytest

from app.audit.failure_projection import Retryability
from app.collection.article_acquisition.errors import (
    AcquisitionError,
    AcquisitionReadError,
    map_origin_to_acquisition,
)
from app.collection.article_acquisition.reader.read_errors import (
    UnreadableResponseError,
    UnreadableResponseReason,
)
from app.collection.external_fetch_errors import (
    ExternalFetchError,
    FetchAccessDeniedError,
    FetchGatewayError,
)
from tests.collection.test_external_fetch_error_codes import (
    _CONSTRUCTION,
    _concrete_subclasses,
)


def test_acquisition_error_marker_base_carries_no_event_stage() -> None:
    assert not hasattr(AcquisitionError, "STAGE")


def test_read_error_derives_external_fetch_retryable_from_origin() -> None:
    """retryable fetch origin (gateway 502) → ``RETRYABLE`` を per-instance で導く。"""
    origin = FetchGatewayError(status_code=502)
    exc = AcquisitionReadError(origin=origin)

    assert isinstance(exc, AcquisitionError)
    assert not hasattr(exc, "STAGE")
    assert exc.FAILURE_KIND == "external_fetch"
    assert exc.RETRYABILITY is Retryability.RETRYABLE
    assert exc.FAILURE_ACTION is None
    assert exc.code == "fetch_gateway_failure"
    assert exc.origin is origin


def test_read_error_derives_external_fetch_non_retryable_from_origin() -> None:
    """terminal fetch origin (403) → 同一クラスが ``NON_RETRYABLE`` を導く。

    上の retryable ケースと matched pair: 1 クラスが両 retryability を origin から
    導く非空虚 witness (片方だけだと導出が空虚に通る)。
    """
    origin = FetchAccessDeniedError(status_code=403, reason="forbidden")
    exc = AcquisitionReadError(origin=origin)

    assert exc.FAILURE_KIND == "external_fetch"
    assert exc.RETRYABILITY is Retryability.NON_RETRYABLE
    assert exc.FAILURE_ACTION is None
    assert exc.code == "fetch_access_denied"
    assert exc.origin is origin


def test_read_error_carries_unreadable_response_reason_code() -> None:
    """read origin → reason.value を ``code`` に、``unreadable_response`` を kind に。

    fetch と同一クラスが origin 型で kind / retryability を分岐する。単一 CODE 定数は
    廃止され code は reason ごとに変わる。read 失敗は全 terminal なので
    ``NON_RETRYABLE`` 固定。
    """
    origin = UnreadableResponseError(
        reason=UnreadableResponseReason.UNEXPECTED_FIELD_SHAPE,
        response_format="json",
        field="items",
    )
    exc = AcquisitionReadError(origin=origin)

    assert exc.FAILURE_KIND == "unreadable_response"
    assert exc.RETRYABILITY is Retryability.NON_RETRYABLE
    assert exc.FAILURE_ACTION is None
    assert exc.code == origin.reason.value == "read_unexpected_field_shape"
    assert exc.origin is origin


@pytest.mark.parametrize(
    ("cls", "kwargs"),
    list(_CONSTRUCTION.items()),
    ids=[c.__name__ for c in _CONSTRUCTION],
)
def test_map_origin_to_acquisition_preserves_fetch_origin_code(
    cls: type[ExternalFetchError],
    kwargs: dict[str, object],
) -> None:
    """全 fetch error が統合 marker に写り、retryability を origin から導く。

    期待 retryability は ``cls.retryable`` (SSoT) から導出しハードコードしない:
    分類の二重定義 (tautology) を避ける。
    """
    origin = cls(**kwargs)  # type: ignore[arg-type]

    marker = map_origin_to_acquisition(origin)

    assert isinstance(marker, AcquisitionReadError)
    assert marker.FAILURE_KIND == "external_fetch"
    assert marker.code == cls.CODE
    assert marker.origin is origin
    expected = Retryability.RETRYABLE if cls.retryable else Retryability.NON_RETRYABLE
    assert marker.RETRYABILITY is expected


def test_map_origin_to_acquisition_maps_unreadable_response() -> None:
    origin = UnreadableResponseError(
        reason=UnreadableResponseReason.MALFORMED_CONTENT, response_format="feed"
    )

    marker = map_origin_to_acquisition(origin)

    assert isinstance(marker, AcquisitionReadError)
    assert marker.FAILURE_KIND == "unreadable_response"
    assert marker.code == "read_malformed_content"
    assert marker.origin is origin


def test_external_fetch_error_family_has_no_stage_policy_attrs() -> None:
    """origin error family は Stage 固有 marker ではないことを固定する。"""
    assert not hasattr(ExternalFetchError, "STAGE")
    for cls in _concrete_subclasses(ExternalFetchError):
        assert not hasattr(cls, "STAGE")
        assert not hasattr(cls, "FAILURE_KIND")
        assert not hasattr(cls, "RETRYABILITY")
        assert not hasattr(cls, "FAILURE_ACTION")
