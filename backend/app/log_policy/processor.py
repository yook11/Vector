"""完成済みルールで入力検査・deny除外・allow選別・ログ値の準備をつなぐ。"""

from collections.abc import MutableMapping
from typing import Any

from app.log_policy.base import LogPolicyRules
from app.log_policy.budget import LogBudgetExceeded, LogEventBudget
from app.log_policy.diagnostics import LogProcessingDiagnostics
from app.log_policy.field_selection import LogFieldSelector
from app.log_policy.logger import PolicyLogger
from app.log_policy.safe_exception_log import extract_exception_fields
from app.log_policy.value_preparation import LogValuePreparer


class LogPolicyProcessor:
    """各処理を順に適用し、失敗時も原文を出さない。"""

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
        selector = LogFieldSelector(rules.allow, rules.deny, diagnostics)
        budget = LogEventBudget()
        preparer = LogValuePreparer(
            deny=rules.deny, mask=rules.mask, budget=budget, diagnostics=diagnostics
        )
        prepared_event: dict[str, Any] = {}
        try:
            for field_name, field_value in event_dict.items():
                budget.check_and_count_log_items(1)
                # トップレベルの項目名を検査し、採用しない項目は値を見ずに落とす。
                if not selector.select(field_name):
                    continue
                # ここまで残った項目だけを採用し、名前の文字数を数えて値を準備する。
                budget.check_and_count_text_chars(len(field_name))
                prepared_event[field_name] = preparer.prepare_field_value(field_value)

            # 例外から安全に抽出した項目は、同名の入力があっても上書きして優先する。
            exception_fields = extract_exception_fields(event_dict.get("exc_info"))
            if exception_fields is not None:
                budget.check_and_count_log_items(len(exception_fields))
                for field_name, field_value in exception_fields.items():
                    budget.check_and_count_text_chars(len(field_name))
                    prepared_event[field_name] = preparer.prepare_field_value(
                        field_value
                    )
            # どのポリシーで処理したログかを、ルール由来の固定値で出力に付ける。
            if rules.policy is not None:
                prepared_event["log_policy"] = rules.policy.value
            prepared_diagnostics = diagnostics.prepare_log_fields(
                mask=rules.mask, budget=budget
            )
            prepared_event.update(prepared_diagnostics)
        except LogBudgetExceeded as exc:
            # 予算オーバー時はログ全体を固定出力へ置換する。
            return {
                "event": "log_policy_budget_exceeded",
                "_policy_limited": True,
                "_policy_limit_reason": exc.reason,
            }
        return prepared_event
