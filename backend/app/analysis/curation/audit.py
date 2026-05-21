"""Stage 3 (curation) 監査 payload の内部 helper。

``CurationAuditRepository`` 内部から呼ばれる private helper。直接の
公開 API ではない (audit_repository の semantic method 経由でのみ使用)。

成功 / 失敗 / DELETE の各経路で共有される「content + source_name の 4 field」を
計算する。``ai_model`` / ``prompt_version`` は本 helper が埋めない —
成功経路は envelope (``call.model_name`` / ``call.prompt_version``) から、
失敗経路は caller が ``curator.model_name`` / ``curator.prompt_version``
property から直接埋める。

content 段階 (`docs/observability/pipeline-events-design.md` §11):
    1. raw — Article.original_content (DB 値)
    2. truncated — raw[:CONTENT_MAX_LENGTH]
    3. sanitized — truncated を sanitize_for_untrusted_block 適用
    4. rendered — TEMPLATE.format() で <untrusted_input> ブロックに埋込済の
       prompt 全体 (本 helper は触らない)

採用規律:
- ``input_content_length``: **段階 1 (raw)** の長さ — truncate 検知のため
- ``input_content_head``: **段階 3 (sanitized)** の先頭 2048 文字 — 実際に
  AI が見たトークンの先頭を表す
- ``input_content_hash``: **段階 3 (sanitized)** 全体の sha256 prefix 16 — 同
  入力の重複検知 / re-curation で同じ入力が再投入されたかの照合
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.analysis.curation.ai.gemini_prompt import GeminiCurationPrompt
from app.analysis.prompt_safety import sanitize_for_untrusted_block

_HEAD_LIMIT = 2048
_HASH_PREFIX_LEN = 16


def base_curation_payload_fields(
    *,
    original_content: str,
    source_name: str | None = None,
) -> dict[str, Any]:
    """Service / tasks.py で共有する curation audit payload の 4 基底 field。

    PR1-a 以降、``ai_model`` / ``prompt_version`` は成功経路では envelope
    (``call.model_name`` / ``call.prompt_version``) から直接埋める方針に
    変わったため、本 helper の戻り値からは外れた。失敗経路 (``append_failure``
    / ``append_drop_article``) は envelope が無い (AI 呼び出し前 or 中の失敗) ため、
    caller 側で ``curator.model_name`` / ``curator.prompt_version`` property
    から個別に埋める。

    Args:
        original_content: ``Article.original_content`` (raw 段階 1)。
        source_name: caller が resolve した source 名 (FK 切断耐性のため
            payload にも保存する)。``None`` の場合は payload key も None。

    Returns:
        ``BasePipelineEventPayload`` / ``ExtractionPayload`` に直接展開可能な
        4 key を含む dict (``source_name``, ``input_content_length``,
        ``input_content_head``, ``input_content_hash``)。
    """
    truncated = original_content[: GeminiCurationPrompt.CONTENT_MAX_LENGTH]
    sanitized = sanitize_for_untrusted_block(truncated)
    return {
        "source_name": source_name,
        "input_content_length": len(original_content),
        "input_content_head": sanitized[:_HEAD_LIMIT],
        "input_content_hash": hashlib.sha256(sanitized.encode("utf-8")).hexdigest()[
            :_HASH_PREFIX_LEN
        ],
    }
