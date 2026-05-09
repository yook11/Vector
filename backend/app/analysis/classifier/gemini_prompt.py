"""Stage 4 (classification) Gemini Prompt — bounded constants + render。

provider-bound Prompt class の Gemini classification 用。``TEMPLATE`` は
provider 共通の ``CLASSIFICATION_PROMPT`` を ClassVar で alias する
(``DeepSeekClassificationPrompt`` も同じ TEMPLATE を share)。

ADR `docs/observability/pipeline-events-design.md` §prompt_version の規律 に従い、
``VERSION`` は class load 時 1 回計算される call signature hash 8 文字。

PR3 で ``RESPONSE_SCHEMA`` を Pydantic class (``ClassificationRawResponse``) →
dict (``CLASSIFICATION_GEMINI_SCHEMA``、SDK Schema 形式 uppercase) に切り替え。
``parse_assessment`` (PR2) が AI 境界の dict → ドメイン詰め替えを 1 箇所に集約
するようになったため、Pydantic 中間型を経由する必要がなくなった。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, ClassVar

from app.analysis.classifier.prompts import CLASSIFICATION_PROMPT
from app.analysis.classifier.schema_tool import CLASSIFICATION_GEMINI_SCHEMA
from app.analysis.prompt_safety import sanitize_for_untrusted_block
from app.observability.prompt_versions import compute_call_signature


class GeminiClassificationPrompt:
    """Stage 4 classification prompt (Gemini 専用)。"""

    TEMPLATE: ClassVar[str] = CLASSIFICATION_PROMPT
    MODEL: ClassVar[str] = "gemini-2.5-flash-lite"
    GEN_CONFIG: ClassVar[Mapping[str, Any]] = MappingProxyType(
        {
            "temperature": 0.2,
            "max_output_tokens": 1024,
            "response_mime_type": "application/json",
        }
    )
    # PR3: Pydantic class → dict (Gemini 専用 SDK Schema 形式)
    RESPONSE_SCHEMA: ClassVar[Mapping[str, Any]] = MappingProxyType(
        CLASSIFICATION_GEMINI_SCHEMA
    )
    SYSTEM_INSTRUCTION: ClassVar[str | None] = None

    VERSION: ClassVar[str] = compute_call_signature(
        prompt_template=TEMPLATE,
        model=MODEL,
        gen_config=GEN_CONFIG,
        response_schema=RESPONSE_SCHEMA,
        system_instruction=SYSTEM_INSTRUCTION,
    )

    @classmethod
    def render(cls, *, title_ja: str, summary_ja: str) -> str:
        """sanitize 済み Stage 1 出力を ``<untrusted_input>`` に埋めて返す。"""
        return cls.TEMPLATE.format(
            title_ja=sanitize_for_untrusted_block(title_ja),
            summary_ja=sanitize_for_untrusted_block(summary_ja),
        )
