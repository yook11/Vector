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
    UnreadableResponseError,
    map_origin_to_acquisition,
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


def test_unreadable_response_error_code_is_stable_read_prefixed() -> None:
    """CODE は接続 family の ``fetch_`` と別カテゴリと分かる ``read_`` prefix。"""
    assert UnreadableResponseError.CODE == "read_unreadable_response"


def test_unreadable_response_error_is_not_a_connection_error() -> None:
    """接続境界 ``ExternalFetchError`` family とは独立した別系統 (継承しない)。"""
    assert not issubclass(UnreadableResponseError, ExternalFetchError)


def test_unreadable_response_error_str_nonempty_without_message() -> None:
    """message 無しでも ``str(exc)`` が非空 (既定 message に CODE を合成)。"""
    assert str(UnreadableResponseError()) == "read_unreadable_response"


def test_unreadable_response_error_explicit_message_takes_precedence() -> None:
    exc = UnreadableResponseError("sitemap parse error: Foo")
    assert str(exc) == "sitemap parse error: Foo"


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


def test_unreadable_response_marker_has_failure_attrs() -> None:
    origin = UnreadableResponseError("rss bozo")
    exc = AcquisitionUnreadableResponseError(origin_error=origin)

    assert exc.FAILURE_KIND == "unreadable_response"
    assert exc.RETRYABILITY is Retryability.NON_RETRYABLE
    assert exc.FAILURE_ACTION is None
    assert exc.code == "read_unreadable_response"
    assert exc.origin_error is origin
    assert str(exc) == (
        "AcquisitionUnreadableResponseError(code='read_unreadable_response')"
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
    origin = UnreadableResponseError("rss bozo")

    marker = map_origin_to_acquisition(origin)

    assert isinstance(marker, AcquisitionUnreadableResponseError)
    assert marker.code == "read_unreadable_response"
    assert marker.origin_error is origin


def test_external_fetch_error_family_has_no_stage_policy_attrs() -> None:
    """origin error family は Stage 固有 marker ではないことを固定する。"""
    assert not hasattr(ExternalFetchError, "STAGE")
    for cls in _concrete_subclasses(ExternalFetchError):
        assert not hasattr(cls, "STAGE")
        assert not hasattr(cls, "FAILURE_KIND")
        assert not hasattr(cls, "RETRYABILITY")
        assert not hasattr(cls, "FAILURE_ACTION")
