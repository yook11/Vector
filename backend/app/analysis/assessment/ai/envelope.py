"""Stage 4 assessor 戻り値 envelope。

Service 層で audit 焼付するために必要な情報 — AI の raw 応答 text、詰め替え前の
``raw_category`` / ``raw_topic``、``prompt_version`` — を assessor 戻り値から
運び上げる。Stage 3 ``ExtractionCall`` と同パターン。

PR2 では dead code として merge し、PR3 で assessor ``_call_api`` の
``parse_assessment`` 経由化と同時に wire-in する。

設計詳細: ``specs/pipeline-events-stage4-assessment.md`` §AssessmentCall envelope
"""

from __future__ import annotations

from dataclasses import dataclass

from app.analysis.assessment.ai.schema import AssessmentResult


@dataclass(frozen=True, slots=True)
class AssessmentCall:
    """assessor の 1 回の API call の結果。

    Service が audit 焼付できるよう、ドメイン詰め替え後の ``result`` に加えて
    raw 応答情報を運ぶ。とりわけ ``OutOfScope`` 経路では ``result`` に
    ``raw_category`` / ``raw_topic`` 情報が落ちるため、「何が ``out_of_scope``
    判定だったのか」を audit に焼くには envelope 経由の運搬が必須。

    Attributes:
        result: ドメイン詰め替え済みの判定結果 (``InScope`` | ``OutOfScope``)。
        raw_response: SDK が返した text 応答 (audit 焼付用、2KB 程度上限想定)。
        raw_category: AI が出力した category slug 値 (詰め替え前、``out_of_scope``
            含む)。enum 化しない (raw は監査用、enum 化すると「妥当な値しか入らない」
            誤解を生む)。
        raw_topic: AI が出力した topic 文字列 (詰め替え前、``OutOfScope`` 経路でも
            保持)。``parse_assessment`` の strict 化により ``str`` 型は保証される。
        prompt_version: 呼び出し元 Prompt class の VERSION (8 文字 hash)。
    """

    result: AssessmentResult
    raw_response: str
    raw_category: str
    raw_topic: str
    prompt_version: str
