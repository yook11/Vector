"""``ExtractionCall`` — extractor 戻り値 envelope (PEP 695 Generic)。

Stage 3 で 1 回の API call 中に確定する全事実 — 詰め替え済み ``result``
(``Signal`` | ``Noise``) / AI の raw 応答 text / 詰め替え前の ``raw_relevance`` /
``prompt_version`` / ``model_name`` — を 1 つの値に集約する。下流
(Repository / AuditRepository) はこの envelope だけ受け取れば、業務 INSERT も
audit 焼付も完結する (caller が ``ai_model`` 等を別経路で引き回さない、
``feedback_bc_boundary_guarantees_downstream``)。

PEP 695 Generic で ``ExtractionCall[Signal]`` / ``ExtractionCall[Noise]`` として
narrow し、``match call: case ExtractionCall(result=Signal())`` 経由で
container 単位の型 narrowing を効かせる (Stage 4 ``AssessmentCall[T]`` と対称)。

raw_response は extraction 監査の S 級情報 (Vector のどこにも残らない極めて
貴重なデバッグ情報、`docs/observability/pipeline-events-design.md` 参照)。
prompt_version は ADR §prompt_version の規律で確定する 8 文字 hash で、
extractor 自身が宣言した値を Service にそのまま伝搬する。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.analysis.extraction.domain import ExtractionResult


@dataclass(frozen=True, slots=True)
class ExtractionCall[T: ExtractionResult]:
    """extractor の 1 回の API call の結果。

    ``T`` は ``Signal`` / ``Noise`` のいずれかに narrow される。Service 層は
    ``match call: case ExtractionCall(result=Signal()):`` で container ごと型を
    絞り、対応する Repository method (``ExtractionRepository.save_signal`` /
    ``ExtractionRepository.save_noise``) に envelope を 1 つ渡すだけで業務
    INSERT + audit 焼付を完結させる。

    Attributes:
        result: ドメイン詰め替え済みの抽出結果 (``Signal`` | ``Noise``)。
        raw_response: SDK が返した text 応答 (audit 焼付用、2KB 程度上限想定)。
        raw_relevance: AI が出力した ``relevance`` 値 (詰め替え前、Stage 4
            ``raw_category`` / ``raw_topic`` と対称。enum 化しない: raw は監査用、
            enum 化すると「妥当な値しか入らない」誤解を生む)。
        prompt_version: 呼び出し元 Prompt class の VERSION (8 文字 hash)。
        model_name: ``BaseExtractor.MODEL`` ClassVar の値。Repository / Audit が
            ``call.model_name`` で参照する (caller が ``ai_model`` を別引数で
            引き回さない、``feedback_bc_boundary_guarantees_downstream``)。
    """

    result: T
    raw_response: str
    raw_relevance: str
    prompt_version: str
    model_name: str
