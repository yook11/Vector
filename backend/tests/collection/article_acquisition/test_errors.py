"""Stage 1 article acquisition 例外の単体テスト。"""

from __future__ import annotations

import pytest

from app.audit.domain.event import Stage
from app.audit.failure_projection import Retryability
from app.collection.article_acquisition.errors import (
    AcquisitionError,
    AcquisitionExternalFetchError,
    AcquisitionUnreadableResponseError,
    SourceAcquisitionError,
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


def test_acquisition_error_is_stage_1_marker_base() -> None:
    assert AcquisitionError.STAGE is Stage.ACQUISITION


def test_external_fetch_marker_derives_retryable_from_origin() -> None:
    """retryable origin (gateway 502) → ``RETRYABLE`` を per-instance で導く。"""
    origin = FetchGatewayError(status_code=502)
    exc = AcquisitionExternalFetchError(origin_error=origin)

    assert isinstance(exc, AcquisitionError)
    assert isinstance(exc, SourceAcquisitionError)
    assert exc.STAGE is Stage.ACQUISITION
    assert exc.FAILURE_KIND == "external_fetch"
    assert exc.RETRYABILITY is Retryability.RETRYABLE
    assert exc.FAILURE_ACTION is None
    assert exc.code == "fetch_gateway_failure"
    assert exc.origin_error is origin
    assert str(exc) == "AcquisitionExternalFetchError(code='fetch_gateway_failure')"


def test_external_fetch_marker_derives_non_retryable_from_origin() -> None:
    """terminal origin (access_denied 403) → 同一クラスが ``NON_RETRYABLE`` を導く。

    上の retryable ケースと matched pair: 1 クラスが両 retryability を origin から
    導く非空虚 witness (片方だけだと導出が空虚に通る)。
    """
    origin = FetchAccessDeniedError(status_code=403, reason="forbidden")
    exc = AcquisitionExternalFetchError(origin_error=origin)

    assert exc.FAILURE_KIND == "external_fetch"
    assert exc.RETRYABILITY is Retryability.NON_RETRYABLE
    assert exc.FAILURE_ACTION is None
    assert exc.code == "fetch_access_denied"
    assert exc.origin_error is origin
    assert str(exc) == "AcquisitionExternalFetchError(code='fetch_access_denied')"


def test_unreadable_response_marker_carries_origin_reason_code() -> None:
    """read marker は origin の reason.value を ``code`` (outcome_code) として運ぶ。

    単一 CODE 定数は廃止され、code は reason ごとに変わる (fetch marker が origin の
    ``CODE`` を運ぶのと同型。read 失敗は全 terminal なので ``NON_RETRYABLE`` 固定)。
    """
    origin = UnreadableResponseError(
        reason=UnreadableResponseReason.UNEXPECTED_FIELD_SHAPE,
        response_format="json",
        field="items",
    )
    exc = AcquisitionUnreadableResponseError(origin_error=origin)

    assert exc.FAILURE_KIND == "unreadable_response"
    assert exc.RETRYABILITY is Retryability.NON_RETRYABLE
    assert exc.FAILURE_ACTION is None
    assert exc.code == origin.reason.value == "read_unexpected_field_shape"
    assert exc.origin_error is origin
    assert str(exc) == (
        "AcquisitionUnreadableResponseError(code='read_unexpected_field_shape')"
    )


@pytest.mark.parametrize(
    ("cls", "kwargs"),
    list(_CONSTRUCTION.items()),
    ids=[c.__name__ for c in _CONSTRUCTION],
)
def test_map_origin_to_acquisition_preserves_origin_code(
    cls: type[ExternalFetchError],
    kwargs: dict[str, object],
) -> None:
    """全 fetch error が統合 marker に写り、retryability を origin から導く。

    期待 retryability は ``cls.retryable`` (SSoT) から導出しハードコードしない:
    分類の二重定義 (tautology) を避ける。
    """
    origin = cls(**kwargs)  # type: ignore[arg-type]

    marker = map_origin_to_acquisition(origin)

    assert isinstance(marker, AcquisitionExternalFetchError)
    assert marker.code == cls.CODE
    assert marker.origin_error is origin
    expected = Retryability.RETRYABLE if cls.retryable else Retryability.NON_RETRYABLE
    assert marker.RETRYABILITY is expected


def test_map_origin_to_acquisition_maps_unreadable_response() -> None:
    origin = UnreadableResponseError(
        reason=UnreadableResponseReason.MALFORMED_CONTENT, response_format="feed"
    )

    marker = map_origin_to_acquisition(origin)

    assert isinstance(marker, AcquisitionUnreadableResponseError)
    assert marker.code == "read_malformed_content"
    assert marker.origin_error is origin


def test_external_fetch_error_family_has_no_stage_policy_attrs() -> None:
    """origin error family は Stage 固有 marker ではないことを固定する。"""
    assert not hasattr(ExternalFetchError, "STAGE")
    for cls in _concrete_subclasses(ExternalFetchError):
        assert not hasattr(cls, "STAGE")
        assert not hasattr(cls, "FAILURE_KIND")
        assert not hasattr(cls, "RETRYABILITY")
        assert not hasattr(cls, "FAILURE_ACTION")
