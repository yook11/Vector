"""Stage 3 (extraction) 監査 payload の組立 helper。

成功 / skip / 失敗 / DELETE の各経路で共有される「6 共通 field」のうち
content 由来 5 field を計算する。``source_name`` は呼出側で resolve した値を
そのまま受け取る (DB session を持たない経路 / 持つ経路で resolve 戦略が
異なるため helper は I/O しない)。

PR3-a-1 では Gemini 専用のため ``GeminiExtractionPrompt`` の ClassVar を直接
参照する。複数 provider をサポートするタイミングで provider-agnostic な
プロンプトメタを引数化する想定 (PR3.5 / PR-Future)。

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
  入力の重複検知 / re-extraction で同じ入力が再投入されたかの照合
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.analysis.extraction.extractor.gemini_prompt import GeminiExtractionPrompt
from app.analysis.prompt_safety import sanitize_for_untrusted_block

_HEAD_LIMIT = 2048
_HASH_PREFIX_LEN = 16


def base_extraction_payload_fields(
    *,
    original_content: str,
    source_name: str | None = None,
) -> dict[str, Any]:
    """Service / tasks.py で共有する extraction audit payload の 6 基底 field。

    Args:
        original_content: ``Article.original_content`` (raw 段階 1)。
        source_name: caller が resolve した source 名 (FK 切断耐性のため
            payload にも保存する)。``None`` の場合は payload key も None。

    Returns:
        ``BasePipelineEventPayload`` / ``ExtractionPayload`` に直接展開可能な
        6 key を含む dict (``source_name``, ``ai_model``, ``prompt_version``,
        ``input_content_length``, ``input_content_head``, ``input_content_hash``)。
    """
    truncated = original_content[: GeminiExtractionPrompt.CONTENT_MAX_LENGTH]
    sanitized = sanitize_for_untrusted_block(truncated)
    return {
        "source_name": source_name,
        "ai_model": GeminiExtractionPrompt.MODEL,
        "prompt_version": GeminiExtractionPrompt.VERSION,
        "input_content_length": len(original_content),
        "input_content_head": sanitized[:_HEAD_LIMIT],
        "input_content_hash": hashlib.sha256(sanitized.encode("utf-8")).hexdigest()[
            :_HASH_PREFIX_LEN
        ],
    }
