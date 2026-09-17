"""目的別定義の登録とポリシー識別子の解決。"""

from collections.abc import Iterable
from types import MappingProxyType

from app.log_policy.policies.ai_inference import AI_INFERENCE_POLICY
from app.log_policy.policies.external_content import EXTERNAL_CONTENT_POLICY
from app.log_policy.rules import LogPolicy, LogPolicyRules

POLICIES = MappingProxyType(
    {
        LogPolicy.AI_INFERENCE: AI_INFERENCE_POLICY,
        LogPolicy.EXTERNAL_CONTENT_FETCH: EXTERNAL_CONTENT_POLICY,
    }
)


class PolicyRegistry:
    """登録時に目的別の禁止を合成し、未登録の識別子は解決しない。"""

    def __init__(self, rules: Iterable[LogPolicyRules] = ()) -> None:
        registered = {}
        for rule in rules:
            purpose = POLICIES.get(rule.policy)
            registered[rule.policy] = LogPolicyRules(
                policy=rule.policy,
                allow=rule.allow,
                deny=rule.deny | (purpose.deny if purpose is not None else frozenset()),
            )
        self._rules = MappingProxyType(registered)

    def resolve(self, declared: object) -> LogPolicyRules | None:
        if not isinstance(declared, LogPolicy):
            return None
        return self._rules.get(declared)
