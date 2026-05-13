"""Stage 4 (assessment) DeepSeek Prompt — bounded text + render。

DeepSeek-V4-Flash bound の Prompt class。``TEMPLATE`` は provider 共通の
``ASSESSMENT_PROMPT`` を ClassVar で alias する (Gemini 版と同 instance を share)。

Gemini との差分は ``MAX_SUMMARY_CHARS = 8000`` の cost guard 1 点 (render の
動作に直接関わる prompt 概念なので Prompt class 側に置く)。

call config (model / gen_config / response_schema / tool_name / base_url /
version / rate_policy) は ``DEEPSEEK_ASSESSMENT_SPEC`` (``spec.py``) が SSoT。
"""

from __future__ import annotations

from typing import ClassVar

from app.analysis.assessment.ai.prompts import ASSESSMENT_PROMPT
from app.analysis.prompt_safety import sanitize_for_untrusted_block


class DeepSeekAssessmentPrompt:
    """Stage 4 assessment prompt (DeepSeek-V4-Flash 専用)。"""

    TEMPLATE: ClassVar[str] = ASSESSMENT_PROMPT

    # Cost guard: 異常に長い summary が来ても per-call output 上限を保つ
    # (Gemini にはない、DeepSeek 固有)
    MAX_SUMMARY_CHARS: ClassVar[int] = 8000

    @classmethod
    def render(cls, *, title_ja: str, summary_ja: str) -> str:
        """sanitize 済み Stage 1 出力を ``<untrusted_input>`` に埋めて返す。

        ``summary_ja`` は ``MAX_SUMMARY_CHARS`` で切り詰めてから sanitize する。
        """
        truncated = summary_ja[: cls.MAX_SUMMARY_CHARS]
        return cls.TEMPLATE.format(
            title_ja=sanitize_for_untrusted_block(title_ja),
            summary_ja=sanitize_for_untrusted_block(truncated),
        )
