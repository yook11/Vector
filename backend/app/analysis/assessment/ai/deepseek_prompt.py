"""Stage 4 (assessment) DeepSeek Prompt — bounded constants + render。

DeepSeek-V4-Flash bound の Prompt class。``TEMPLATE`` は provider 共通の
``ASSESSMENT_PROMPT`` を ClassVar で alias する (Gemini 版と同 instance を share)。

Gemini との差分:
- ``RESPONSE_SCHEMA`` は **dict (tool schema)** で、Pydantic class ではない。
  DeepSeek の Function Calling + strict mode は ``$ref``/``$defs`` を enforce
  しないため、inline flat な JSON Schema を渡す必要がある
  (`schema_tool.ASSESSMENT_TOOL_SCHEMA`)。
- ``MAX_SUMMARY_CHARS = 8000`` で input cost guard (Gemini にはない)。

ADR `docs/observability/pipeline-events-design.md` §prompt_version の規律 に従い、
``VERSION`` は class load 時 1 回計算される call signature hash 8 文字。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, ClassVar

from app.analysis.assessment.ai.prompts import ASSESSMENT_PROMPT
from app.analysis.assessment.ai.schema_tool import ASSESSMENT_TOOL_SCHEMA
from app.analysis.prompt_safety import sanitize_for_untrusted_block
from app.observability.prompt_versions import compute_call_signature


class DeepSeekAssessmentPrompt:
    """Stage 4 assessment prompt (DeepSeek-V4-Flash 専用)。"""

    # Function Calling の関数名 (tool_choice + tools.function.name で参照)
    TOOL_NAME: ClassVar[str] = "assess_article"

    TEMPLATE: ClassVar[str] = ASSESSMENT_PROMPT
    MODEL: ClassVar[str] = "deepseek-v4-flash"
    GEN_CONFIG: ClassVar[Mapping[str, Any]] = MappingProxyType(
        {
            "max_tokens": 512,
            "tool_choice": {
                "type": "function",
                "function": {"name": TOOL_NAME},
            },
            # DeepSeek 独自: Stage 4 はシンプル分類なので reasoning trace 不要
            "extra_body": {"thinking": {"type": "disabled"}},
        }
    )
    # SDK は dict をそのまま受ける (Pydantic 経由ではない)
    RESPONSE_SCHEMA: ClassVar[Mapping[str, Any]] = MappingProxyType(
        ASSESSMENT_TOOL_SCHEMA
    )
    SYSTEM_INSTRUCTION: ClassVar[str | None] = None

    # Cost guard: 異常に長い summary が来ても per-call output 上限を保つ
    # (Gemini にはない、DeepSeek 固有)
    MAX_SUMMARY_CHARS: ClassVar[int] = 8000

    VERSION: ClassVar[str] = compute_call_signature(
        prompt_template=TEMPLATE,
        model=MODEL,
        gen_config=GEN_CONFIG,
        response_schema=RESPONSE_SCHEMA,
        system_instruction=SYSTEM_INSTRUCTION,
    )

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
