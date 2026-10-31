"""完成済みルールで入力検査・deny除外・allow選別・ログ値の準備をつなぐ。"""

from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any, Literal

from structlog.typing import WrappedLogger

from app.log_policy.base import LogPolicyRules
from app.log_policy.budget import (
    BudgetLimitReason,
    LogBudgetExceeded,
    LogEventBudget,
    UncountedBudget,
)
from app.log_policy.diagnostics import LogProcessingDiagnostics
from app.log_policy.exceptions.conversion import convert_exception
from app.log_policy.exceptions.extraction import extract_exception_fields
from app.log_policy.exceptions.types import ExceptionConverter
from app.log_policy.field_selection import LogFieldSelector
from app.log_policy.value_preparation import LogValuePreparer

type ReplacementReason = (
    BudgetLimitReason | Literal["json_serialization_failed", "processing_failed"]
)


def replaced_log(
    event_dict: Mapping[str, Any],
    *,
    log_policy: str | None,
    reason: ReplacementReason,
) -> dict[str, Any]:
    """ログを出せないときに、入力に由来しない値と理由だけを残したログを返す。"""
    replaced: dict[str, Any] = {"event": "log_policy_replaced"}
    # level・timestamp はチェーンの前段が上書きして入れる値なので、検査せずに写す。
    for key in ("level", "timestamp"):
        if key in event_dict:
            replaced[key] = event_dict[key]
    if log_policy is not None:
        replaced["log_policy"] = log_policy
    replaced["_policy_replaced_reason"] = reason
    return replaced


# 生成後に規則を差し替えられないよう、属性の代入を禁止する。
@dataclass(frozen=True)
class LogPolicyProcessor:
    """各処理を順に適用し、失敗時も原文を出さない。"""

    rules: LogPolicyRules
    exception_converter: ExceptionConverter = field(
        default=convert_exception, kw_only=True
    )

    def __post_init__(self) -> None:
        if type(self.rules) is not LogPolicyRules:
            raise TypeError("rules must be LogPolicyRules")

    def __call__(
        self,
        logger: WrappedLogger,
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        try:
            return self._process(event_dict, rules=self.rules)
        except LogBudgetExceeded as exc:
            reason: ReplacementReason = exc.reason
        except Exception:
            reason = "processing_failed"
        policy = self.rules.policy
        return replaced_log(
            event_dict,
            log_policy=None if policy is None else policy.value,
            reason=reason,
        )

    def _process(
        self,
        event_dict: MutableMapping[str, Any],
        *,
        rules: LogPolicyRules,
    ) -> dict[str, Any]:
        """準備済みのログを返し、共有予算の超過は例外のまま呼び出し側へ伝える。"""
        diagnostics = LogProcessingDiagnostics()
        budget = LogEventBudget()
        selector = LogFieldSelector(rules.allow, rules.deny, diagnostics, budget=budget)
        preparer = LogValuePreparer(
            deny=rules.deny,
            mask=rules.mask,
            sanitize=rules.sanitize,
            budget=budget,
            diagnostics=diagnostics,
        )
        prepared_event: dict[str, Any] = {}
        # まずセレクターがトップレベルの項目を選別、denyを除いたログを返す。
        selected_fields = selector.select_fields(event_dict)
        # 整理したログを検証していく。
        for field_name, field_value in selected_fields.items():
            prepared_event[field_name] = preparer.prepare_field_value(
                field_value, field_name=field_name
            )

        # 同名の通常入力より、実際の例外から抽出した情報を優先する。
        exception_fields = extract_exception_fields(
            event_dict.get("exc_info"),
            exception_converter=self.exception_converter,
        )
        if exception_fields is not None:
            # 例外の出力の量は例外の変換の上限で決まるため、予算では数えない。
            exception_preparer = LogValuePreparer(
                deny=rules.deny,
                mask=rules.mask,
                sanitize=rules.sanitize,
                budget=UncountedBudget(),
                diagnostics=diagnostics,
            )
            for field_name, field_value in exception_fields.items():
                prepared_event[field_name] = exception_preparer.prepare_field_value(
                    field_value, field_name=field_name
                )
        # どのポリシーで処理したログかを、出力に残す。
        if rules.policy is not None:
            prepared_event["log_policy"] = rules.policy.value
        prepared_diagnostics = diagnostics.prepare_log_fields(budget=budget)
        prepared_event.update(prepared_diagnostics)
        return prepared_event
