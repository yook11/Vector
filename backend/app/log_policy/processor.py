"""ポリシー解決・選別・値保護を structlog に接続する。"""

from collections.abc import Iterable, MutableMapping
from typing import Any

from app.log_policy.registry import PolicyRegistry
from app.log_policy.rules import BASE_DENY, LogPolicyRules
from app.log_policy.safe_exception_log import extract_safe_exception_fields
from app.log_policy.selection import POLICY_BIND_KEY, select_fields
from app.log_policy.value_protection import ValueProtector


class LogPolicyProcessor:
    """各処理を順に適用し、失敗時も原文を出さない。"""

    def __init__(self, rules: Iterable[LogPolicyRules] = ()) -> None:
        self._registry = PolicyRegistry(rules)

    def __call__(
        self,
        logger: Any,
        method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        try:
            return self._process(event_dict)
        except Exception:
            return {"event": "log_policy_failed", "_policy_error": "processing_failed"}

    def _process(self, event_dict: MutableMapping[str, Any]) -> dict[str, Any]:
        rules = self._registry.resolve(event_dict.get(POLICY_BIND_KEY))
        deny = rules.effective_deny if rules is not None else BASE_DENY
        selection = select_fields(event_dict, rules)
        protector = ValueProtector(deny)
        out = protector.protect_fields(selection.fields)
        exception = extract_safe_exception_fields(event_dict.get("exc_info"), deny=deny)
        if exception is not None:
            out.update(exception)
        if rules is not None:
            out["log_policy"] = rules.policy.value
        out.update(selection.diagnostics())
        if protector.denied:
            out["_denied_nested_count"] = protector.denied
        return out
