"""完成済みルールで入力検査・deny除外・allow選別・ログ値の準備をつなぐ。"""

from collections.abc import MutableMapping
from typing import Any

from app.log_policy.base import LogPolicyRules
from app.log_policy.budget import LogBudgetExceeded, LogEventBudget
from app.log_policy.diagnostics import LogProcessingDiagnostics
from app.log_policy.exceptions.conversion import convert_exception
from app.log_policy.exceptions.extraction import extract_exception_fields
from app.log_policy.exceptions.types import ExceptionConverter
from app.log_policy.field_selection import LogFieldSelector
from app.log_policy.logger import PolicyLogger
from app.log_policy.value_preparation import (
    DEPTH_LIMIT,
    EXCEPTION_DEPTH_LIMIT,
    LogValuePreparer,
)


class LogPolicyProcessor:
    """各処理を順に適用し、失敗時も原文を出さない。"""

    def __init__(
        self,
        *,
        exception_converter: ExceptionConverter = convert_exception,
    ) -> None:
        self._exception_converter = exception_converter

    def __call__(
        self,
        logger: PolicyLogger,
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        try:
            return self._process(event_dict, rules=logger.rules)
        except Exception:
            return {"event": "log_policy_failed", "_policy_error": "processing_failed"}

    def _process(
        self,
        event_dict: MutableMapping[str, Any],
        *,
        rules: LogPolicyRules,
    ) -> dict[str, Any]:
        """準備済みのログを返し、共有予算の超過時はログ全体を固定出力へ置換する。"""
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
        try:
            # まずセレクターがトップレベルの項目を選別、denyを除いたログを返す。
            selected_fields = selector.select_fields(event_dict)
            # 整理したログを検証していく。
            for field_name, field_value in selected_fields.items():
                prepared_event[field_name] = preparer.prepare_field_value(
                    field_value, field_name=field_name, depth_limit=DEPTH_LIMIT
                )

            # 同名の通常入力より、実際の例外から抽出した情報を優先する。
            exception_fields = extract_exception_fields(
                event_dict.get("exc_info"),
                exception_converter=self._exception_converter,
            )
            if exception_fields is not None:
                budget.check_and_count_log_items(len(exception_fields))
                for field_name, field_value in exception_fields.items():
                    budget.check_and_count_text_chars(len(field_name))
                    prepared_event[field_name] = preparer.prepare_field_value(
                        field_value,
                        field_name=field_name,
                        depth_limit=EXCEPTION_DEPTH_LIMIT,
                    )
            # どのポリシーで処理したログかを、出力に残す。
            if rules.policy is not None:
                prepared_event["log_policy"] = rules.policy.value
            prepared_diagnostics = diagnostics.prepare_log_fields(budget=budget)
            prepared_event.update(prepared_diagnostics)
        except LogBudgetExceeded as exc:
            # 予算オーバー時はログ全体を固定出力へ置換する。
            return {
                "event": "log_policy_budget_exceeded",
                "_policy_limited": True,
                "_policy_limit_reason": exc.reason,
            }
        return prepared_event
