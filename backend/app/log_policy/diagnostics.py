"""ログ一件の検査・選別で起きたことを記録する。"""

from typing import Any


class LogProcessingDiagnostics:
    """各工程の診断を記録し、ログの項目として返す。"""

    def __init__(self) -> None:
        self._denied_keys: list[str] = []
        self._denied_nested_count = 0
        self._unregistered_count = 0

    def record_top_level_denied(self, key: str) -> None:
        self._denied_keys.append(key)

    def record_unregistered(self) -> None:
        self._unregistered_count += 1

    def record_nested_denied(self) -> None:
        self._denied_nested_count += 1

    def as_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if self._denied_keys:
            fields["_denied_keys"] = list(self._denied_keys)
        if self._unregistered_count:
            fields["_unregistered_count"] = self._unregistered_count
        if self._denied_nested_count:
            fields["_denied_nested_count"] = self._denied_nested_count
        return fields
