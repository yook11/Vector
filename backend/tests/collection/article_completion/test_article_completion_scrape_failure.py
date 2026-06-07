"""scrape concern (Stage 1: Fetch + HTML 抽出) の分類 mapper テスト。

Stage 2 (完成段) の分類は ``test_article_completion_completion_failure.py`` が
所有する。本ファイルは scrape の Retry 軸分類のみを検証する。

構造保証 (spec 完了条件): retryable=True の全 ``ExternalFetchError`` concrete
subclass が stage2 の backoff schedule (schedule 別の固定割当 ∪ instance state で
分岐する ``FetchOriginServerError`` / ``FetchRateLimitedError``) を割当済みである
こと。新 retryable subclass を期待表に登録し忘れると本テストが落ちる。terminal は
origin error 自身の ``retryable`` 属性 (SSoT) から導出し、family の分割被覆
(retryable/terminal の CODE 集合) は ``test_external_fetch_error_codes.py`` が所有する。

期待表 ``_EXPECTED_SCHEDULE_BY_TYPE`` は production の写像 (``classify_*`` 内の
``match``) を import せず spec から手書きする (tautology 回避)。schedule テンプレート
(``BLIP`` 等) は比較対象として import するが、type→schedule の割当判断はテストが
独立に持つ。``_CONSTRUCT`` は ``test_external_fetch_error_codes.py`` の構築表と同形だが
解いている問題が違う (CODE 契約 vs decision 分割) ため共有しない。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.collection.article_completion.retry_policy import (
    BLIP,
    OUTAGE,
    TIMEOUT,
    UNKNOWN,
    FixedDelay,
    RetrySchedule,
    ScheduleDelay,
)
from app.collection.article_completion.scrape_failure import (
    ScrapeContentQualityTooLow,
    ScrapeFailure,
    ScrapeNotHtml,
    ScrapeParseCrashed,
    ScrapeParserGaveUp,
    ScrapeRetryable,
    ScrapeTerminal,
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


# retryable origin error → stage2 backoff schedule の期待写像 (spec から手書き)。
# 固定割当グループ。instance state で分岐する 2 型は別途個別検証する。
_EXPECTED_SCHEDULE_BY_TYPE: dict[type[ExternalFetchError], RetrySchedule] = {
    FetchGatewayError: BLIP,
    FetchNetworkError: BLIP,
    FetchTimeoutError: TIMEOUT,
    FetchRequestTimeoutError: UNKNOWN,
    FetchRetryableStatusError: UNKNOWN,
    FetchUnexpectedStatusError: UNKNOWN,
}

# ``Retry-After`` / reason を読むため schedule を instance state で選ぶ retryable。
_STATE_DEPENDENT_RETRYABLE: set[type[ExternalFetchError]] = {
    FetchOriginServerError,
    FetchRateLimitedError,
}

# terminal は origin error 自身の ``retryable`` 属性 (SSoT) から導出する。
_TERMINAL_SUBCLASSES: list[type[ExternalFetchError]] = sorted(
    (c for c in _concrete_subclasses(ExternalFetchError) if not c.retryable),
    key=lambda c: c.__name__,
)

_RETRYABLE_MAPPING_CASES: list[tuple[type[ExternalFetchError], RetrySchedule]] = sorted(
    _EXPECTED_SCHEDULE_BY_TYPE.items(), key=lambda kv: kv[0].__name__
)


class TestPartitionStructuralGuarantee:
    """retryable 属性 (SSoT) と stage2 の backoff schedule 割当の整合を固定する。"""

    def test_construct_table_covers_all_subclasses(self) -> None:
        assert set(_CONSTRUCT) == _concrete_subclasses(ExternalFetchError)

    def test_classification_groups_are_pairwise_disjoint(self) -> None:
        terminal = set(_TERMINAL_SUBCLASSES)
        scheduled = set(_EXPECTED_SCHEDULE_BY_TYPE)
        assert terminal.isdisjoint(scheduled)
        assert terminal.isdisjoint(_STATE_DEPENDENT_RETRYABLE)
        assert scheduled.isdisjoint(_STATE_DEPENDENT_RETRYABLE)

    def test_every_retryable_subclass_has_a_schedule(self) -> None:
        # 別不変条件 (family 分割被覆は test_external_fetch_error_codes が所有):
        # retryable=True の全 origin error が固定割当 ∪ state 分岐のどちらかに属す。
        # 新 retryable subclass を期待表に登録し忘れると等式が破れて落ちる。
        declared = set(_EXPECTED_SCHEDULE_BY_TYPE) | _STATE_DEPENDENT_RETRYABLE
        retryable = {c for c in _concrete_subclasses(ExternalFetchError) if c.retryable}
        assert declared == retryable


@pytest.mark.parametrize(
    "cls",
    _TERMINAL_SUBCLASSES,
    ids=[c.__name__ for c in _TERMINAL_SUBCLASSES],
)
def test_terminal_fetch_error_maps_to_terminal_with_code(
    cls: type[ExternalFetchError],
) -> None:
    """terminal (retryable=False) は ``ScrapeTerminal(reason_code=cls.CODE)`` になる。"""
    assert classify_external_fetch_error(_build(cls)) == ScrapeTerminal(
        reason_code=cls.CODE
    )


@pytest.mark.parametrize(
    "cls,schedule",
    _RETRYABLE_MAPPING_CASES,
    ids=[c.__name__ for c, _ in _RETRYABLE_MAPPING_CASES],
)
def test_retryable_fetch_error_maps_to_expected_schedule(
    cls: type[ExternalFetchError],
    schedule: RetrySchedule,
) -> None:
    """固定割当グループは期待 schedule の cap + delay を持つ ``ScrapeRetryable``。"""
    assert classify_external_fetch_error(_build(cls)) == ScrapeRetryable(
        reason_code=cls.CODE,
        max_attempts=schedule.max_attempts,
        next_delay=schedule.delay,
    )


class TestFetchOriginServerErrorExplicitBranch:
    """``FetchOriginServerError`` は instance state で delay を選ぶ明示ケース。"""

    def test_service_unavailable_with_retry_after_uses_fixed_delay(self) -> None:
        exc = FetchOriginServerError(
            status_code=503,
            reason="service_unavailable",
            retry_after_seconds=120.0,
        )
        assert classify_external_fetch_error(exc) == ScrapeRetryable(
            reason_code="fetch_origin_server_error",
            max_attempts=OUTAGE.max_attempts,
            next_delay=FixedDelay(120.0),
        )

    def test_service_unavailable_without_retry_after_uses_outage_schedule(self) -> None:
        exc = FetchOriginServerError(
            status_code=503, reason="service_unavailable", retry_after_seconds=None
        )
        assert classify_external_fetch_error(exc) == ScrapeRetryable(
            reason_code="fetch_origin_server_error",
            max_attempts=OUTAGE.max_attempts,
            next_delay=OUTAGE.delay,
        )

    def test_internal_error_uses_outage_schedule(self) -> None:
        exc = FetchOriginServerError(status_code=500, reason="internal_error")
        assert classify_external_fetch_error(exc) == ScrapeRetryable(
            reason_code="fetch_origin_server_error",
            max_attempts=OUTAGE.max_attempts,
            next_delay=OUTAGE.delay,
        )

    def test_internal_error_ignores_retry_after_seconds(self) -> None:
        # reason gate が service_unavailable 以外なので retry_after は載せない。
        exc = FetchOriginServerError(
            status_code=500, reason="internal_error", retry_after_seconds=30.0
        )
        assert classify_external_fetch_error(exc) == ScrapeRetryable(
            reason_code="fetch_origin_server_error",
            max_attempts=OUTAGE.max_attempts,
            next_delay=OUTAGE.delay,
        )


class TestFetchRateLimitedExplicitBranch:
    """``FetchRateLimitedError`` (429) の ``Retry-After`` 尊重 (バグ修正の正本)。

    旧実装は 429 を type 表で引き instance state を読めず ``Retry-After`` を黙って
    捨てていた (503 のみ救済され非対称)。現実装は server 指示を ``FixedDelay`` で
    尊重し、無ければ ``UNKNOWN`` schedule に倒す。cap は現状維持の ``UNKNOWN``。
    """

    def test_with_retry_after_uses_fixed_delay(self) -> None:
        exc = FetchRateLimitedError(status_code=429, retry_after_seconds=90.0)
        decision = classify_external_fetch_error(exc)
        assert decision == ScrapeRetryable(
            reason_code="fetch_rate_limited",
            max_attempts=UNKNOWN.max_attempts,
            next_delay=FixedDelay(90.0),
        )

    def test_with_retry_after_does_not_drop_server_hint(self) -> None:
        # 回帰の核: server 指示が schedule に潰されないことを非空虚に踏む。
        exc = FetchRateLimitedError(status_code=429, retry_after_seconds=90.0)
        decision = classify_external_fetch_error(exc)
        assert isinstance(decision, ScrapeRetryable)
        assert decision.next_delay == FixedDelay(90.0)
        assert decision.next_delay != UNKNOWN.delay

    def test_without_retry_after_uses_unknown_schedule(self) -> None:
        exc = FetchRateLimitedError(status_code=429, retry_after_seconds=None)
        assert classify_external_fetch_error(exc) == ScrapeRetryable(
            reason_code="fetch_rate_limited",
            max_attempts=UNKNOWN.max_attempts,
            next_delay=UNKNOWN.delay,
        )


@pytest.mark.parametrize(
    "failure,expected_reason_code,expected_detail",
    [
        (
            ScrapeNotHtml(content_type="application/pdf"),
            "scrape_not_html",
            "content_type=application/pdf",
        ),
        (
            ScrapeParserGaveUp(),
            "scrape_parser_gave_up",
            None,
        ),
        (
            ScrapeParseCrashed(error_class="ValueError", error_message="bad parse"),
            "scrape_parse_crashed",
            "ValueError: bad parse",
        ),
        (
            ScrapeContentQualityTooLow(
                body_length=0, title_present=False, body_sample=None
            ),
            "scrape_content_quality_too_low",
            "body_length=0 title_present=False",
        ),
        (
            ScrapeContentQualityTooLow(
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
    assert result == ScrapeTerminal(
        reason_code=expected_reason_code, detail=expected_detail
    )


class TestTransportErrorDelegation:
    """transport (origin error) は ``classify_external_fetch_error`` に委譲し、
    保持する例外の class+message を ``detail`` に畳む (retryable がありうる)。"""

    def test_terminal_fetch_error_folds_into_terminal_with_detail(self) -> None:
        # 404 は terminal 集合。reason_code は exc.CODE 素通し、detail に class 名。
        err = FetchResourceNotFoundError(status_code=404, reason="not_found")
        result = classify_scrape_failure(err)
        assert isinstance(result, ScrapeTerminal)
        assert result.reason_code == err.CODE
        assert result.detail is not None
        assert result.detail.startswith("FetchResourceNotFoundError")

    def test_retryable_fetch_error_folds_into_retryable_with_detail(self) -> None:
        # 502 は BLIP schedule の retryable。content 失敗と違い terminal に落とさない。
        err = FetchGatewayError(status_code=502)
        result = classify_scrape_failure(err)
        assert isinstance(result, ScrapeRetryable)
        assert result.reason_code == err.CODE
        assert result.max_attempts == BLIP.max_attempts
        assert result.next_delay == BLIP.delay
        assert result.detail is not None
        assert result.detail.startswith("FetchGatewayError")


class _SyntheticRetryable:
    """match のどの case にも当たらない retryable をシミュレートする probe。

    ``case _`` (未登録 retryable の保守 fallback) が非空虚に効くことを確認する。
    実 ``ExternalFetchError`` subclass にすると ``__subclasses__()`` registry を
    プロセス越しに汚染し、他モジュールの totality assert
    (``test_external_fetch_error_codes``) を壊すため、継承せず duck-typed にする。
    """

    retryable = True
    CODE = "synthetic_retryable_test_only"


def test_unregistered_retryable_falls_back_to_unknown() -> None:
    """どの case にも当たらない retryable は ``_`` で ``UNKNOWN`` schedule に倒れる。"""
    decision = classify_external_fetch_error(_SyntheticRetryable())  # type: ignore[arg-type]
    assert decision == ScrapeRetryable(
        reason_code="synthetic_retryable_test_only",
        max_attempts=UNKNOWN.max_attempts,
        next_delay=UNKNOWN.delay,
    )


class TestScrapeRetryableDecisionMethods:
    """``ScrapeRetryable`` が再投入の決定 (打ち切り / 次回 ready_at) を純粋に答える。

    handler はこの答えを実行 (I/O) するだけで内部を覗かない (Feature Envy 解消)。
    本クラスは DB を介さない純粋契約のみを検証し、期待値はテスト所有の delay /
    cap から導く (production テンプレートに依存しない)。
    """

    def test_is_exhausted_at_max_attempts_boundary(self) -> None:
        # 境界を非空虚に踏む: max ちょうどで打ち切り、直前は継続。
        retryable = ScrapeRetryable(
            reason_code="x", max_attempts=3, next_delay=ScheduleDelay((1.0,))
        )
        assert retryable.is_exhausted(3) is True
        assert retryable.is_exhausted(2) is False

    def test_next_ready_at_uses_schedule_delay(self) -> None:
        now = datetime(2026, 5, 25, tzinfo=UTC)
        retryable = ScrapeRetryable(
            reason_code="x", max_attempts=5, next_delay=ScheduleDelay((3.0, 7.0))
        )
        # 1 回目失敗 → schedule[0]=3 分後。
        assert retryable.next_ready_at(now=now, attempt_count=1) == now + timedelta(
            minutes=3
        )

    def test_next_ready_at_with_fixed_delay_ignores_attempt(self) -> None:
        now = datetime(2026, 5, 25, tzinfo=UTC)
        retryable = ScrapeRetryable(
            reason_code="x", max_attempts=5, next_delay=FixedDelay(120.0)
        )
        # FixedDelay=120s=2 分。attempt が進んでも server 指示で固定 (非空虚)。
        assert retryable.next_ready_at(now=now, attempt_count=9) == now + timedelta(
            minutes=2
        )
