"""Stage 4 (assessment) Gemini Prompt — bounded text + render。

provider-bound Prompt class の Gemini assessment 用。``TEMPLATE`` は
provider 共通の ``ASSESSMENT_PROMPT`` を ClassVar で alias する
(``DeepSeekAssessmentPrompt`` も同じ TEMPLATE を share)。

call config (model / gen_config / response_schema / system_instruction /
version / rate_policy) は ``GEMINI_ASSESSMENT_SPEC`` (``spec.py``) が SSoT。
本 class は Prompt 文面と render (sanitize) のみに責務を絞る。
"""

from __future__ import annotations

from typing import ClassVar

from app.analysis.assessment.ai.prompts import ASSESSMENT_PROMPT
from app.analysis.prompt_safety import sanitize_for_untrusted_block


class GeminiAssessmentPrompt:
    """Stage 4 assessment prompt (Gemini 専用)。"""

    TEMPLATE: ClassVar[str] = ASSESSMENT_PROMPT

    @classmethod
    def render(cls, *, title_ja: str, summary_ja: str) -> str:
        """sanitize 済み Stage 1 出力を ``<untrusted_input>`` に埋めて返す。"""
        return cls.TEMPLATE.format(
            title_ja=sanitize_for_untrusted_block(title_ja),
            summary_ja=sanitize_for_untrusted_block(summary_ja),
        )
