"""acquisition concern (Stage 1: Fetch + HTML 抽出) の分類 mapper テスト。

Stage 2 (完成段) の分類は ``test_article_completion_completion_failure.py`` が
所有する。本ファイルは acquisition の Retry 軸分類のみを検証する。

構造保証 (spec 完了条件): 全 ``ExternalFetchError`` concrete subclass が
(Terminal 集合 ∪ policy 別 Retryable ∪ ``FetchOriginServerError`` 明示分岐) で
**過不足なく** 分割される。subclass を追加して分類し忘れると本テストが落ちる。

``_CONSTRUCT`` は ``test_external_fetch_error_codes.py`` の構築表と同形だが、
解いている問題が違う (CODE 契約 vs decision 分割) ため共有しない。
"""

from __future__ import annotations

import pytest

from app.collection.article_completion.acquisition_failure import (
    _RETRYABLE_FETCH_ERROR_TYPES_BY_POLICY,
    _TERMINAL_FETCH_ERROR_TYPES,
    AcquisitionFailure,
    NotHtml,
    ParseCrashed,
    ParserRejected,
    QualityGateFailed,
    Retryable,
    Terminal,
    classify_acquisition_failure,
    classify_external_fetch_error,
)
from app.collection.article_completion.retry_policy import (
    OUTAGE_POLICY,
    RETRY_AFTER_POLICY,
    RetryPolicy,
)
from app.collection.external_fetch_errors import (
    ExternalFetchError,
    FetchAccessDeniedError,
    FetchContentTypeMismatchError,
    FetchGatewayError,
    FetchLegalBlockError,
    FetchNetworkError,
    FetchOriginServerError,
    FetchParseError,
    FetchRateLimitedError,
    FetchRedirectBlockedError,
    FetchRedirectLoopError,
    FetchRequestTimeoutError,
    FetchResourceNotFoundError,
    FetchResponseTooLargeError,
    FetchRetryableStatusError,
    FetchRobotsDisallowedError,
    FetchRobotsUnavailableError,
    FetchSsrfBlockedError,
    FetchTimeoutError,
    FetchUnexpectedStatusError,
)

# 各 concrete subclass を最小 kwargs で構築する表。新 subclass を追加して
# 登録し忘れると ``test_construct_table_covers_all_subclasses`` が落ちる。
_CONSTRUCT: dict[type[ExternalFetchError], dict[str, object]] = {
    FetchAccessDeniedError: {"status_code": 403, "reason": "forbidden"},
    FetchLegalBlockError: {},
    FetchResourceNotFoundError: {"status_code": 404, "reason": "not_found"},
    FetchRateLimitedError: {},
    FetchOriginServerError: {"status_code": 500, "reason": "internal_error"},
    FetchGatewayError: {"status_code": 502},
    FetchRequestTimeoutError: {},
    FetchRetryableStatusError: {"status_code": 425},
    FetchUnexpectedStatusError: {"status_code": 418},
    FetchTimeoutError: {},
    FetchNetworkError: {},
    FetchSsrfBlockedError: {},
    FetchRobotsDisallowedError: {},
    FetchRobotsUnavailableError: {},
    FetchRedirectBlockedError: {},
    FetchRedirectLoopError: {},
    FetchResponseTooLargeError: {},
    FetchContentTypeMismatchError: {
        "expected_content_type": "text/html",
        "detected_content_type": None,
    },
    FetchParseError: {},
}


def _concrete_subclasses(root: type) -> set[type]:
    """``root`` の subclass を再帰的に集める (将来の中間 subclass にも追従)。"""
    found: set[type] = set()
    for sub in root.__subclasses__():
        found.add(sub)
        found |= _concrete_subclasses(sub)
    return found


def _build(cls: type[ExternalFetchError]) -> ExternalFetchError:
    return cls(**_CONSTRUCT[cls])  # type: ignore[arg-type]


class TestPartitionStructuralGuarantee:
    """3 分類グループが全 concrete subclass を過不足なく分割すること。"""

    def test_construct_table_covers_all_subclasses(self) -> None:
        assert set(_CONSTRUCT) == _concrete_subclasses(ExternalFetchError)

    def test_classification_groups_are_pairwise_disjoint(self) -> None:
        terminal = set(_TERMINAL_FETCH_ERROR_TYPES)
        retryable = {
            t
            for types in _RETRYABLE_FETCH_ERROR_TYPES_BY_POLICY.values()
            for t in types
        }
        explicit = {FetchOriginServerError}
        assert terminal.isdisjoint(retryable)
        assert terminal.isdisjoint(explicit)
        assert retryable.isdisjoint(explicit)

    def test_classification_covers_every_concrete_subclass(self) -> None:
        # subclass 追加で分類漏れ → この等式が破れてテストが落ちる (構造保証)。
        terminal = set(_TERMINAL_FETCH_ERROR_TYPES)
        retryable = {
            t
            for types in _RETRYABLE_FETCH_ERROR_TYPES_BY_POLICY.values()
            for t in types
        }
        explicit = {FetchOriginServerError}
        assert terminal | retryable | explicit == _concrete_subclasses(
            ExternalFetchError
        )


@pytest.mark.parametrize(
    "cls",
    list(_TERMINAL_FETCH_ERROR_TYPES),
    ids=[c.__name__ for c in _TERMINAL_FETCH_ERROR_TYPES],
)
def test_terminal_fetch_error_maps_to_terminal_with_code(
    cls: type[ExternalFetchError],
) -> None:
    """terminal グループは ``Terminal(reason_code=cls.CODE)`` になる。"""
    assert classify_external_fetch_error(_build(cls)) == Terminal(reason_code=cls.CODE)


@pytest.mark.parametrize(
    "policy,cls",
    [
        (policy, cls)
        for policy, types in _RETRYABLE_FETCH_ERROR_TYPES_BY_POLICY.items()
        for cls in types
    ],
    ids=[
        f"{policy.code}-{cls.__name__}"
        for policy, types in _RETRYABLE_FETCH_ERROR_TYPES_BY_POLICY.items()
        for cls in types
    ],
)
def test_retryable_fetch_error_maps_to_its_policy(
    policy: RetryPolicy,
    cls: type[ExternalFetchError],
) -> None:
    """retryable グループは所属 policy 付き ``Retryable`` になる。"""
    assert classify_external_fetch_error(_build(cls)) == Retryable(
        reason_code=cls.CODE, policy=policy
    )


class TestFetchOriginServerErrorExplicitBranch:
    """``FetchOriginServerError`` は instance state で分岐する明示ケース。"""

    def test_service_unavailable_with_retry_after_uses_retry_after_policy(
        self,
    ) -> None:
        exc = FetchOriginServerError(
            status_code=503,
            reason="service_unavailable",
            retry_after_seconds=120.0,
        )
        assert classify_external_fetch_error(exc) == Retryable(
            reason_code="fetch_origin_server_error",
            policy=RETRY_AFTER_POLICY,
            retry_after_seconds=120.0,
        )

    def test_service_unavailable_without_retry_after_uses_outage_policy(self) -> None:
        exc = FetchOriginServerError(
            status_code=503, reason="service_unavailable", retry_after_seconds=None
        )
        assert classify_external_fetch_error(exc) == Retryable(
            reason_code="fetch_origin_server_error", policy=OUTAGE_POLICY
        )

    def test_internal_error_uses_outage_policy(self) -> None:
        exc = FetchOriginServerError(status_code=500, reason="internal_error")
        assert classify_external_fetch_error(exc) == Retryable(
            reason_code="fetch_origin_server_error", policy=OUTAGE_POLICY
        )

    def test_internal_error_ignores_retry_after_seconds(self) -> None:
        # reason gate が service_unavailable 以外なので retry_after は載せない。
        exc = FetchOriginServerError(
            status_code=500, reason="internal_error", retry_after_seconds=30.0
        )
        assert classify_external_fetch_error(exc) == Retryable(
            reason_code="fetch_origin_server_error", policy=OUTAGE_POLICY
        )


@pytest.mark.parametrize(
    "failure,expected_reason_code,expected_detail",
    [
        (
            NotHtml(content_type="application/pdf"),
            "acquisition_not_html",
            "content_type=application/pdf",
        ),
        (
            ParserRejected(),
            "acquisition_parser_rejected",
            None,
        ),
        (
            ParseCrashed(error_class="ValueError", error_message="bad parse"),
            "acquisition_crashed",
            "ValueError: bad parse",
        ),
        (
            QualityGateFailed(body_length=0, title_present=False, body_sample=None),
            "acquisition_quality_gate",
            "body_length=0 title_present=False",
        ),
        (
            QualityGateFailed(
                body_length=12, title_present=True, body_sample="too short"
            ),
            "acquisition_quality_gate",
            "body_length=12 title_present=True sample='too short'",
        ),
    ],
)
def test_acquisition_failure_maps_to_terminal_with_evidence_detail(
    failure: AcquisitionFailure,
    expected_reason_code: str,
    expected_detail: str | None,
) -> None:
    """各 variant が ``acquisition_*`` terminal + 証拠 detail を持つ。"""
    result = classify_acquisition_failure(failure)
    assert result == Terminal(reason_code=expected_reason_code, detail=expected_detail)
