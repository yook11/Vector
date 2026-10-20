"""Briefing LLM 呼出の stage marker 例外。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar


class BriefingError(Exception):
    """Briefing 系処理の基底例外。"""


class BriefingConfigurationError(BriefingError):
    """設定不整合 (API key 未設定等)。retry しても解決しないため fail-fast。"""

    CODE: ClassVar[str] = "briefing_generation_llm_configuration_invalid"


class BriefingLlmError(BriefingError):
    """LLM provider 呼出由来の一時失敗。"""

    CODE: ClassVar[str] = "briefing_generation_llm_provider_call_failed"

    provider_error: BaseException

    def __init__(self, *, provider_error: BaseException) -> None:
        super().__init__(str(provider_error) or self.CODE)
        self.provider_error = provider_error


class BriefingLlmResponseInvalidError(BriefingError):
    """LLM 応答が briefing schema / article id 制約に合致しない。

    ``violations`` は「どの field のどの制約が壊れたか」(loc + 制約種別) の列挙。
    str(exc) に焼き込まれ、例外のメッセージからそのまま原因が読める。
    LLM 出力の値そのものは含めない (untrusted 内容を log へ流さない)。
    """

    CODE: ClassVar[str] = "briefing_generation_llm_response_contract_invalid"

    violations: tuple[str, ...]

    def __init__(self, *, violations: Sequence[str] = ()) -> None:
        self.violations = tuple(violations)
        detail = "; ".join(self.violations)
        super().__init__(f"{self.CODE}: {detail}" if detail else self.CODE)
