"""Stage 3 (curation) 監査 payload の caller 側 pre-compute helper。

curation Service / failure_handling から呼ばれ、戻り値の ``CurationAuditInput``
を ``CurationAuditRepository.append_*`` の kwargs に展開する。``source_name`` の
resolve は audit 側 (``_resolve_source_name(article_id)``) の責務で、本 helper は
扱わない。

これにより ``app/audit/stages/curation.py`` から本 module への逆方向 import を
撤去し、``app/audit/__init__.py`` の依存方向宣言
(collection / analysis / insights → audit の片方向) と整合させる。

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
from typing import TypedDict

from app.analysis.curation.ai.gemini_prompt import GeminiCurationPrompt
from app.analysis.prompt_safety import sanitize_for_untrusted_block

_HEAD_LIMIT = 2048
_HASH_PREFIX_LEN = 16


class CurationAuditInput(TypedDict):
    """``CurationAuditRepository.append_*`` の kwargs として渡す 3 field SSoT。

    ``build_curation_audit_input(...)`` の戻り値型。caller が dict として持つ間も
    型が締まり、audit_repository 側の引数とも整合する。
    """

    input_content_length: int
    input_content_head: str
    input_content_hash: str


def build_curation_audit_input(*, original_content: str) -> CurationAuditInput:
    """curation audit row に詰める input content 3 field を計算する。

    caller (curation Service / failure_handling) が AI 呼び出し前後に呼び、
    戻り値を ``CurationAuditRepository.append_*`` の kwargs に展開する。
    sanitize / truncate は AI 呼び出し前処理 (``GeminiCurationPrompt`` /
    ``sanitize_for_untrusted_block``) と同じ仕様で再計算する (Curator 内部の
    sanitize 結果が caller に戻らないため)。

    ``source_name`` は audit_repository 側で ``_resolve_source_name(article_id)``
    から DB 経由で resolve するため本 helper は扱わない。
    """
    truncated = original_content[: GeminiCurationPrompt.CONTENT_MAX_LENGTH]
    sanitized = sanitize_for_untrusted_block(truncated)
    return CurationAuditInput(
        input_content_length=len(original_content),
        input_content_head=sanitized[:_HEAD_LIMIT],
        input_content_hash=hashlib.sha256(sanitized.encode("utf-8")).hexdigest()[
            :_HASH_PREFIX_LEN
        ],
    )
