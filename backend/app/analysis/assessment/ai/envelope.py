"""Stage 4 assessor 戻り値 envelope。

Stage 4 で 1 回の API call 中に確定する全事実 — 詰め替え済み ``result`` /
AI の raw 応答 text / 詰め替え前の ``raw_category`` / ``raw_topic`` /
``prompt_version`` / ``model_name`` — を 1 つの値に集約する。
下流 (Repository / AuditRepository) はこの envelope だけ受け取れば、
業務 INSERT も audit 焼付も完結する (caller が ``ai_model`` 等を別経路で
引き回す必要がない)。

PEP 695 Generic で ``AssessmentCall[InScope]`` / ``AssessmentCall[OutOfScope]``
として narrow し、``match call: case AssessmentCall(result=InScope())`` 経由で
container 単位の型 narrowing を効かせる。

設計詳細: ``specs/pipeline-events-stage4-assessment.md`` §AssessmentCall envelope
"""

from __future__ import annotations

from dataclasses import dataclass

from app.analysis.assessment.domain.result import AssessmentResult


@dataclass(frozen=True, slots=True)
class AssessmentCall[T: AssessmentResult]:
    """assessor の 1 回の API call の結果。

    ``T`` は ``InScope`` / ``OutOfScope`` のいずれかに narrow される。Service 層は
    ``match call: case AssessmentCall(result=InScope()):`` で container ごと型を
    絞り、対応する Repository method (``save_in_scope`` / ``save_out_of_scope``)
    に envelope を 1 つ渡すだけで業務 INSERT + audit 焼付を完結させる。

    Attributes:
        result: ドメイン詰め替え済みの判定結果 (``InScope`` | ``OutOfScope``)。
        raw_response: SDK が返した text 応答 (audit 焼付用、2KB 程度上限想定)。
        raw_category: AI が出力した category slug 値 (詰め替え前、``out_of_scope``
            含む)。enum 化しない (raw は監査用、enum 化すると「妥当な値しか入らない」
            誤解を生む)。
        raw_topic: AI が出力した topic 文字列 (詰め替え前、``OutOfScope`` 経路でも
            保持)。``parse_assessment`` の strict 化により ``str`` 型は保証される。
        prompt_version: 呼び出し元 Prompt class の VERSION (8 文字 hash)。
        model_name: ``BaseAssessor.MODEL`` ClassVar の値。Repository / Audit が
            ``call.model_name`` で参照する (caller が ``ai_model`` を別引数で
            引き回さない、`feedback_bc_boundary_guarantees_downstream`)。
    """

    result: T
    raw_response: str
    raw_category: str
    raw_topic: str
    prompt_version: str
    model_name: str
