"""AI 利用枠の枯渇を CloudWatch A6 alarm へ知らせる EMF 打点 (stage 中立)。

枯渇 (残高切れ・利用枠消尽) は残高チャージ等の運用者対応が必須の事象。発生の
たびに素直に 1 打点 emit し、通知の重複抑制は alarm の状態遷移に委ねる。一時的
rate limit (時間経過で回復) は対象外。kind には provider CODE をそのまま使い、
stage hold reason / audit outcome_code と同一語彙で突き合わせられるようにする。
"""

from __future__ import annotations

from app.ai_providers.errors import (
    AIProviderInsufficientBalanceError,
    AIProviderUsageLimitExhaustedError,
)
from app.cloudwatch.emf import emit_metric

type ExhaustedProviderError = (
    AIProviderInsufficientBalanceError | AIProviderUsageLimitExhaustedError
)


def exhausted_provider_error(
    exc: BaseException | None,
) -> ExhaustedProviderError | None:
    """既存の枯渇通知に該当する例外を副作用なく取り出す。"""
    if isinstance(
        exc, AIProviderInsufficientBalanceError | AIProviderUsageLimitExhaustedError
    ):
        return exc
    return None


def record_ai_provider_exhausted(exc: BaseException | None, *, provider: str) -> None:
    """枯渇系 provider error なら ``ai_provider_exhausted`` を 1 打点 emit する。

    枯渇以外 (None 含む) は no-op。エラー分類が確定する境界の所有者
    (failure handler / agent runtime) が呼ぶ。
    """
    exhausted = exhausted_provider_error(exc)
    if exhausted is None:
        return
    emit_metric(
        "ai_provider_exhausted",
        dimensions={"kind": exhausted.CODE, "provider": provider},
        value=1,
        unit="Count",
    )
