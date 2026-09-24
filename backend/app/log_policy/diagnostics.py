"""ログ一件の検査・選別で起きたことを記録する。"""

from typing import Any

from app.log_policy.budget import TEXT_LIMIT, LogEventBudget
from app.log_policy.leak_prevention import prevent_credential_leaks


class LogProcessingDiagnostics:
    """各工程の診断を記録し、診断自身をログに出せる形へ整える。"""

    def __init__(self) -> None:
        self._denied_keys: list[str] = []
        self._denied_nested_count = 0
        self._unregistered_count = 0
        self._policy_limited = False

    def record_top_level_denied(self, key: str) -> None:
        self._denied_keys.append(key)

    def record_unregistered(self) -> None:
        self._unregistered_count += 1

    def record_top_level_limit_reached(self) -> None:
        self._policy_limited = True

    def record_nested_denied(self) -> None:
        self._denied_nested_count += 1

    def record_nested_invalid_key(self) -> None:
        self._denied_nested_count += 1

    def record_nested_key_limit_reached(self) -> None:
        self._policy_limited = True

    def prepare_log_fields(self, *, budget: LogEventBudget) -> dict[str, Any]:
        """入力由来の診断を共有予算で検査し、情報漏洩防止を適用して返す。"""
        diagnostic_fields = self.as_fields()
        if not self._denied_keys:
            return diagnostic_fields

        budget.check_and_count_log_items(1)
        budget.check_and_count_text_chars(len("_denied_keys"))
        for key in self._denied_keys:
            budget.check_and_count_log_items(1)
            if len(key) <= TEXT_LIMIT:
                budget.check_and_count_text_chars(len(key))

        # 一覧全体の予算検査が終わるまで、入力由来のキー名の出力準備をしない。
        prepared_keys: list[str] = []
        for key in self._denied_keys:
            if len(key) > TEXT_LIMIT:
                prepared_keys.append("[limit]")
                continue
            prepared_keys.append(prevent_credential_leaks(key))
        diagnostic_fields["_denied_keys"] = prepared_keys
        return diagnostic_fields

    def as_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if self._denied_keys:
            fields["_denied_keys"] = list(self._denied_keys)
        if self._unregistered_count:
            fields["_unregistered_count"] = self._unregistered_count
        if self._policy_limited:
            fields["_policy_limited"] = True
        if self._denied_nested_count:
            fields["_denied_nested_count"] = self._denied_nested_count
        return fields
