"""scrape concern (Stage 1: Fetch + HTML 抽出) の分類 mapper テスト。

Stage 2 (完成段) の分類は ``test_article_completion_completion_failure.py`` が
所有する。本ファイルは scrape の Retry 軸分類のみを検証する。

構造保証 (spec 完了条件): 全 ``ExternalFetchError`` concrete subclass が
(Terminal 集合 ∪ policy 別 Retryable ∪ ``FetchOriginServerError`` 明示分岐) で
**過不足なく** 分割される。subclass を追加して分類し忘れると本テストが落ちる。

``_CONSTRUCT`` は ``test_external_fetch_error_codes.py`` の構築表と同形だが、
解いている問題が違う (CODE 契約 vs decision 分割) ため共有しない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.collection.article_completion.retry_policy import (
    BLIP_POLICY,
    OUTAGE_POLICY,
    RETRY_AFTER_POLICY,
    TIMEOUT_POLICY,
    RetryPolicy,
)
from app.collection.article_completion.scrape_failure import (
    _RETRYABLE_FETCH_ERROR_TYPES_BY_POLICY,
    _TERMINAL_FETCH_ERROR_TYPES,
    ContentQualityTooLow,
    FetchFailed,
    NotHtml,
    ParseCrashed,
    ParserGaveUp,
    Retryable,
    ScrapeFailure,
    Terminal,
    classify_external_fetch_error,
    classify_scrape_failure,
)
from app.collection.external_fetch_errors import (
    ExternalFetchError,
    FetchAccessDeniedError,
    FetchContentTypeMismatchError,
    FetchGatewayError,
    FetchLegalBlockError,
    FetchNetworkError,
    FetchOriginServerError,
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
            "scrape_not_html",
            "content_type=application/pdf",
        ),
        (
            ParserGaveUp(),
            "scrape_parser_gave_up",
            None,
        ),
        (
            ParseCrashed(error_class="ValueError", error_message="bad parse"),
            "scrape_parse_crashed",
            "ValueError: bad parse",
        ),
        (
            ContentQualityTooLow(body_length=0, title_present=False, body_sample=None),
            "scrape_content_quality_too_low",
            "body_length=0 title_present=False",
        ),
        (
            ContentQualityTooLow(
                body_length=12, title_present=True, body_sample="too short"
            ),
            "scrape_content_quality_too_low",
            "body_length=12 title_present=True sample='too short'",
        ),
    ],
)
def test_scrape_failure_maps_to_terminal_with_evidence_detail(
    failure: ScrapeFailure,
    expected_reason_code: str,
    expected_detail: str | None,
) -> None:
    """各 content variant が ``scrape_*`` terminal + 証拠 detail を持つ。"""
    result = classify_scrape_failure(failure)
    assert result == Terminal(reason_code=expected_reason_code, detail=expected_detail)


class TestFetchFailedDelegation:
    """transport variant ``FetchFailed`` は ``classify_external_fetch_error`` に委譲し、
    保持する例外の class+message を ``detail`` に畳む (retryable がありうる)。"""

    def test_terminal_fetch_error_folds_into_terminal_with_detail(self) -> None:
        # 404 は terminal 集合。reason_code は exc.CODE 素通し、detail に class 名。
        err = FetchResourceNotFoundError(status_code=404, reason="not_found")
        result = classify_scrape_failure(FetchFailed(error=err))
        assert isinstance(result, Terminal)
        assert result.reason_code == err.CODE
        assert result.detail is not None
        assert result.detail.startswith("FetchResourceNotFoundError")

    def test_retryable_fetch_error_folds_into_retryable_with_detail(self) -> None:
        # 502 は BLIP policy の retryable。content 失敗と違い terminal に落とさない。
        err = FetchGatewayError(status_code=502)
        result = classify_scrape_failure(FetchFailed(error=err))
        assert isinstance(result, Retryable)
        assert result.reason_code == err.CODE
        assert result.policy == BLIP_POLICY
        assert result.detail is not None
        assert result.detail.startswith("FetchGatewayError")


class TestRetryableDecisionMethods:
    """``Retryable`` が再投入の決定 (打ち切り / 次回 ready_at) を純粋に答える。

    handler はこの答えを実行 (I/O) するだけで policy 内部を覗かない
    (Feature Envy 解消)。本クラスは DB を介さない純粋契約のみを検証する。
    """

    def test_is_exhausted_at_max_attempts_boundary(self) -> None:
        # 境界を非空虚に踏む: max ちょうどで打ち切り、直前は継続。
        retryable = Retryable(reason_code="x", policy=TIMEOUT_POLICY)
        assert retryable.is_exhausted(TIMEOUT_POLICY.max_attempts) is True
        assert retryable.is_exhausted(TIMEOUT_POLICY.max_attempts - 1) is False

    def test_next_ready_at_uses_policy_schedule_without_server_hint(self) -> None:
        now = datetime(2026, 5, 25, tzinfo=UTC)
        retryable = Retryable(reason_code="x", policy=TIMEOUT_POLICY)
        # 1 回目失敗 → schedule[0] 分後。
        expected = now + timedelta(minutes=TIMEOUT_POLICY.delay_minutes_schedule[0])
        assert retryable.next_ready_at(now=now, attempt_count=1) == expected

    def test_next_ready_at_prefers_server_retry_after(self) -> None:
        now = datetime(2026, 5, 25, tzinfo=UTC)
        # server 指示 120s=2 分が policy schedule[0]=5 分より優先される (非空虚)。
        retryable = Retryable(
            reason_code="x", policy=OUTAGE_POLICY, retry_after_seconds=120.0
        )
        assert retryable.next_ready_at(now=now, attempt_count=1) == now + timedelta(
            minutes=2
        )

    def test_policy_code_exposes_policy_identifier(self) -> None:
        assert Retryable(reason_code="x", policy=OUTAGE_POLICY).policy_code == "outage"
