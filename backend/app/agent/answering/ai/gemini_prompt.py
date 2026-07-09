"""Gemini evidence answer prompt renderer."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from app.agent.answering.evidence import AnswerEvidenceItem
from app.analysis.prompt_safety import sanitize_for_untrusted_block

EVIDENCE_ANSWER_PROMPT = """# Role
あなたは Vector の evidence-grounded answer synthesizer です。

# Task
ユーザー質問に対し、与えられた evidence だけを引用根拠として日本語で回答してください。

# Hard Rules
- cited_refs には answer 本文の citation marker に出した source_ref だけを
  重複なしで入れる。
- answered の場合は cited_refs を 1 件以上にし、missing_aspects は空にする。
- insufficient の場合は missing_aspects を 1 件以上にする。
- 引用できる根拠が無い場合は、その旨を明確に断ったうえで、
  一般知識に基づく参考回答を述べ、断定を避ける。
- evidence にない事実を、引用付きの確認済み事実として扱わない。
- answer は必ずユーザーに表示されるため、insufficient でも有用な範囲で簡潔に答える。
- 下記の会話文脈は回答の形だけを決める。事実の根拠は evidence だけに限定する。

# Citation Rules
- answer 本文では、根拠に基づく文または節の直後に citation marker を付ける。
- marker 形式は [[source_ref]] のみ。例: [[1]]
- citation marker は句点の後に置く。例: 売上は増加しました。[[1]]
- 複数の根拠が同じ主張を支える場合は連続して置く。例: 需要は強いです。[[1]][[2]]
- sufficiency が insufficient の場合でも、根拠に基づく文には citation marker を付ける。
- evidence block に存在しない source_ref を絶対に使わない。
- evidence にない事実を、引用付きの確認済み事実として書かない。
- References / Sources セクションは作らない。

# Output
JSON object only:
{{
  "sufficiency": "answered" | "insufficient",
  "answer": "string",
  "cited_refs": ["source_ref"],
  "missing_aspects": ["string"]
}}

# Context
as_of: {as_of}
target_time_window: {target_time_window}

# User Question
<untrusted_input>
{question}
</untrusted_input>

# Conversation Context
<untrusted_input>
user_intent: {user_intent}
prior_coverage: {prior_coverage}
user_activity_context: {user_activity_context}
</untrusted_input>

# Evidence
{evidence}
"""

EVIDENCE_ANSWER_REPAIR_PROMPT = """

# Repair Context
前回の出力は回答合成 schema validation に失敗しました。
同じ質問と evidence に対して、次のエラーを直した JSON object だけを返してください。

<untrusted_input>
{previous_error}
</untrusted_input>
"""

_NO_EVIDENCE_BLOCK = (
    "引用できる evidence は 0 件です。cited_refs は空にし、"
    "sufficiency は insufficient にしてください。citation marker を書かないでください。"
)


class GeminiEvidenceAnswerPrompt:
    """Evidence answer prompt for Gemini."""

    TEMPLATE: ClassVar[str] = EVIDENCE_ANSWER_PROMPT

    @classmethod
    def render(
        cls,
        *,
        question: str,
        evidence: list[AnswerEvidenceItem],
        as_of: datetime,
        target_time_window: str | None,
        user_intent: str = "",
        prior_coverage: str = "",
        user_activity_context: str = "",
        previous_error: str | None = None,
    ) -> str:
        prompt = cls.TEMPLATE.format(
            question=sanitize_for_untrusted_block(question),
            evidence=_render_evidence(evidence),
            as_of=as_of.isoformat(),
            target_time_window=target_time_window or "",
            user_intent=sanitize_for_untrusted_block(user_intent),
            prior_coverage=sanitize_for_untrusted_block(prior_coverage),
            user_activity_context=sanitize_for_untrusted_block(user_activity_context),
        )
        if previous_error is None:
            return prompt
        return prompt + EVIDENCE_ANSWER_REPAIR_PROMPT.format(
            previous_error=sanitize_for_untrusted_block(previous_error)
        )


def _render_evidence(evidence: list[AnswerEvidenceItem]) -> str:
    if not evidence:
        return _NO_EVIDENCE_BLOCK
    return "\n\n".join(_render_evidence_item(item) for item in evidence)


def _render_evidence_item(item: AnswerEvidenceItem) -> str:
    source = item.source
    parts = [
        f"[{sanitize_for_untrusted_block(source.source_ref)}]",
        f"kind: {source.kind}",
        f"title: {sanitize_for_untrusted_block(source.title)}",
    ]
    url = getattr(source, "url", None)
    if url is not None:
        parts.append(f"url: {sanitize_for_untrusted_block(str(url))}")
    article_id = getattr(source, "article_id", None)
    if article_id is not None:
        parts.append(f"article_id: {article_id}")
    if source.published_at is not None:
        parts.append(f"published_at: {source.published_at.isoformat()}")
    if source.kind == "external_url":
        if source.source_name:
            parts.append(
                f"source_name: {sanitize_for_untrusted_block(source.source_name)}"
            )
        parts.append(f"claim: {sanitize_for_untrusted_block(source.evidence_claim)}")
    parts.append("text:")
    parts.append("<untrusted_input>")
    parts.append(sanitize_for_untrusted_block(item.text))
    parts.append("</untrusted_input>")
    return "\n".join(parts)
