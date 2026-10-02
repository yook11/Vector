"""``record_ai_provider_exhausted`` の CloudWatch A6 alarm 向け EMF emit 契約。

枯渇系 provider error (残高不足 / 利用枠消尽) だけが ``ai_provider_exhausted`` を
emit し、それ以外 (一時的 rate limit・非 provider error・None) は no-op であることを
固定する (specs/observability/cloudwatch-alerting.md)。EMF 構造自体
(Namespace/Unit/value 型) の正本は ``tests/cloudwatch/test_emf.py``。
"""

from __future__ import annotations

import pytest

from app.ai_providers.errors import (
    AIProviderErrorResponseError,
    AIProviderErrorResponseReason,
)
from app.analysis.ai_provider_exhaustion import record_ai_provider_exhausted
from tests.cloudwatch.records import metric_records

_METRIC = "ai_provider_exhausted"


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (
            AIProviderErrorResponseError(
                reason=AIProviderErrorResponseReason.INSUFFICIENT_BALANCE,
                status_code=402,
            ),
            "insufficient_balance",
        ),
        (
            AIProviderErrorResponseError(
                reason=AIProviderErrorResponseReason.QUOTA_EXHAUSTED, status_code=429
            ),
            "quota_exhausted",
        ),
    ],
    ids=["insufficient_balance", "quota_exhausted"],
)
def test_exhausted_provider_error_emits_metric_with_reason_as_kind(
    exc: AIProviderErrorResponseError,
    kind: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """枯渇の 2 理由は reason を ``kind`` にした 1 打点を Count=1 で emit する。"""
    record_ai_provider_exhausted(exc, provider="gemini")

    records = metric_records(capsys.readouterr().out, _METRIC)
    assert len(records) == 1
    record = records[0]
    metric_def = record["_aws"]["CloudWatchMetrics"][0]
    assert metric_def["Namespace"] == "Vector/Pipeline"
    assert metric_def["Dimensions"] == [["kind", "provider"]]
    assert metric_def["Metrics"] == [{"Name": _METRIC, "Unit": "Count"}]
    assert record[_METRIC] == 1
    assert record["kind"] == kind
    assert record["provider"] == "gemini"


def test_provider_dimension_is_the_caller_supplied_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``provider`` dimension は呼び出し側が渡した値をそのまま運ぶ。"""
    record_ai_provider_exhausted(
        AIProviderErrorResponseError(
            reason=AIProviderErrorResponseReason.QUOTA_EXHAUSTED, status_code=429
        ),
        provider="deepseek",
    )

    record = metric_records(capsys.readouterr().out, _METRIC)[0]
    assert record["provider"] == "deepseek"


def test_rate_limited_is_recoverable_by_waiting_and_does_not_emit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """一時的 rate limit (時間経過で回復) は枯渇ではないため emit しない。"""
    record_ai_provider_exhausted(
        AIProviderErrorResponseError(
            reason=AIProviderErrorResponseReason.RATE_LIMITED, status_code=429
        ),
        provider="gemini",
    )

    assert metric_records(capsys.readouterr().out, _METRIC) == []


def test_other_state_error_not_in_exhausted_set_does_not_emit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """枯渇系以外の プロバイダー例外（設定不正等） は emit しない。"""
    record_ai_provider_exhausted(
        AIProviderErrorResponseError(
            reason=AIProviderErrorResponseReason.AUTH, status_code=401
        ),
        provider="gemini",
    )

    assert metric_records(capsys.readouterr().out, _METRIC) == []


def test_non_provider_exception_does_not_emit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """provider error 階層に属さない例外は emit しない。"""
    record_ai_provider_exhausted(ValueError("surprise"), provider="gemini")

    assert metric_records(capsys.readouterr().out, _METRIC) == []


def test_none_does_not_emit(capsys: pytest.CaptureFixture[str]) -> None:
    """exc が None (未分類 / 対象外境界) のときは emit しない。"""
    record_ai_provider_exhausted(None, provider="gemini")

    assert metric_records(capsys.readouterr().out, _METRIC) == []
