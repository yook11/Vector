"""独自型や list の派生型を受けても、本番の renderer で原文へ戻らない。"""

from __future__ import annotations

import pytest

from app.log_policy import LogPolicy, LogPolicyRules
from app.log_policy.processor import LogPolicyProcessor
from app.log_policy.runtime import render_policy_json

pytestmark = pytest.mark.unit

_SECRET = "synthetic private value"
_RULES = LogPolicyRules(LogPolicy.PROCESS_RUNTIME, frozenset({"payload"}))


class TestUnsupportedValues:
    """本番のrendererは独自オブジェクトを文字列にせず、検査していない要素を出さない。"""

    def test_arbitrary_objects_are_not_stringified(self) -> None:
        """独自オブジェクトとbytesを受けても、本番のrendererはreprやstructlogフックで文字列にせず原文を出さない。"""

        class Unsafe:
            def __repr__(self):
                return _SECRET

            def __structlog__(self):
                return _SECRET

        event = {"event": "failed", "payload": [Unsafe(), _SECRET.encode()]}
        output = LogPolicyProcessor(_RULES)(None, "error", event)
        rendered = render_policy_json(None, "error", output)
        assert _SECRET not in rendered

    def test_list_subclass_outputs_only_inspected_items(self) -> None:
        """走査のたびに中身を変えるlistの派生型を受けても、検査した要素だけを出力する。"""

        class ChangingList(list):
            iterations = 0

            def __iter__(self):
                self.iterations += 1
                return iter(["safe"] if self.iterations == 1 else [_SECRET])

        output = LogPolicyProcessor(_RULES)(
            None,
            "info",
            {"event": "failed", "payload": ChangingList()},
        )
        rendered = render_policy_json(None, "info", output)
        assert _SECRET not in rendered
