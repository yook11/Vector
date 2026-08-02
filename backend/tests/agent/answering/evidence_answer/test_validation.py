"""Evidence answer draft finalization tests(S4)。

finalize_evidence_answer_draft は plain text の answer 本文だけを受け取り、
本文の citation marker から EvidenceAnswerDraft を決定的に組み立てる
(raw JSON 入力・requirement_ids・defects list は撤去される)。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.answering.evidence_answer.contract import (
    EvidenceAnswerDraft,
    EvidenceAnswerDraftInvalidError,
)
from app.agent.answering.evidence_answer.evidence import AnswerEvidenceItem
from app.agent.answering.evidence_answer.validation import (
    finalize_evidence_answer_draft,
)
from app.agent.contract import ExternalUrlSource


def _evidence(source_ref: str = "1") -> AnswerEvidenceItem:
    return AnswerEvidenceItem(
        source=ExternalUrlSource(
            source_ref=source_ref,
            url="https://example.com/source",
            title="source",
            evidence_claim="確認済みの主張",
        ),
        text="確認済みの根拠本文",
    )


def _finalize(answer: str, *, evidence: list[AnswerEvidenceItem]) -> Any:
    """S4: finalize_evidence_answer_draft(answer, *, evidence) -> EvidenceAnswerDraft
    (raw JSON入力・requirement_ids・defects listは撤去される)。"""
    try:
        return finalize_evidence_answer_draft(answer, evidence=evidence)
    except TypeError:
        pytest.fail(
            "S4: finalize_evidence_answer_draft must accept "
            "(answer: str, *, evidence: list[AnswerEvidenceItem]) "
            "-> EvidenceAnswerDraft"
        )


def test_finalizes_answer_with_markers_into_first_use_order_deduplicated_refs() -> None:
    """条件5: 本文の[[N]]からcited_refsが初出順・重複排除で算出される。"""
    answer = "根拠から確認できます。[[2]][[1]] 再度触れます。[[2]]"

    draft = _finalize(answer, evidence=[_evidence("1"), _evidence("2")])

    assert draft == EvidenceAnswerDraft(answer=answer, cited_refs=["2", "1"])


def test_marker_parse_boundaries_use_double_bracket_digits_only() -> None:
    """marker解釈規則(二重角括弧+数字のみ)は現行を維持する。"""
    answer = (
        "単括弧 [1] はmarkerではありません。[[1]] "
        "digit以外の [[a]] もmarkerになりません。"
    )

    draft = _finalize(answer, evidence=[_evidence("1")])

    assert draft.cited_refs == ["1"]


def test_rejects_answer_with_citation_ref_unknown_to_evidence() -> None:
    """条件6: evidenceに存在しないrefを本文が参照したdraftは不正。"""
    with pytest.raises(EvidenceAnswerDraftInvalidError, match=r"\[\[2\]\]"):
        _finalize("不実在の引用です。[[2]]", evidence=[_evidence("1")])


def test_rejects_answer_without_marker_when_evidence_is_non_empty() -> None:
    """条件7: evidenceが非空でmarkerが無い本文は不正(retryはflow側の責務)。"""
    with pytest.raises(EvidenceAnswerDraftInvalidError):
        _finalize("引用がありません。", evidence=[_evidence()])


def test_accepts_answer_without_marker_when_evidence_is_empty() -> None:
    """条件8: evidenceが空でmarkerが無い本文は正常に成立し、cited_refsは空になる
    (sourcesはcited_refsだけから組み立てられるため、これでsources空を担保する)。
    """
    answer = "引用できる根拠がありません。"

    draft = _finalize(answer, evidence=[])

    assert draft == EvidenceAnswerDraft(answer=answer, cited_refs=[])


def test_rejects_answer_with_marker_when_evidence_is_empty() -> None:
    """条件9: evidenceが空でmarkerがある本文は不正
    (evidenceに存在しないrefとして既存の検証が弾く。回帰ガード)。
    """
    with pytest.raises(EvidenceAnswerDraftInvalidError):
        _finalize("根拠がないのに引用しています。[[1]]", evidence=[])


@pytest.mark.parametrize("answer", ["", "   ", "\n\t"])
def test_rejects_blank_answer_as_draft_invalid_not_pydantic(answer: str) -> None:
    """条件4: 空白のみの本文はdraft不正として扱われる
    (pydanticのNonBlankText違反ではなくEvidenceAnswerDraftInvalidErrorであることを、
    別の例外型を許さないpytest.raisesの型指定そのもので担保する)。
    """
    with pytest.raises(EvidenceAnswerDraftInvalidError):
        _finalize(answer, evidence=[])


def test_repeated_markers_in_a_single_sentence_are_deduplicated() -> None:
    answer = "同じ根拠を複数回引用します。[[1]] 別の文でも使います。[[1]]"

    draft = _finalize(answer, evidence=[_evidence("1")])

    assert draft.cited_refs == ["1"]


def test_consecutive_markers_are_all_recognized() -> None:
    answer = "連続 marker を使います。[[1]][[2]]"

    draft = _finalize(answer, evidence=[_evidence("1"), _evidence("2")])

    assert draft.cited_refs == ["1", "2"]


# --- グループ形 marker 受理 (spec: agent-citation-marker-grouped-refs-slice.md) ---
# 正準形 [[N]] に加えて、モデルが自然に出すグループ形 [[1], [2]] を受理する。
# 正準形の既存挙動は変えない (加算的変更)。


def test_group_marker_expands_refs_in_written_order() -> None:
    """設計判断2: グループ形 [[1], [2]] は書かれた順に ref 列へ展開される。"""
    answer = "複数の根拠を示します。[[1], [2]]"

    draft = _finalize(answer, evidence=[_evidence("1"), _evidence("2")])

    assert draft.cited_refs == ["1", "2"]


def test_group_marker_mixed_with_consecutive_markers_dedupes_in_first_use_order() -> (
    None
):
    """グループ形と連続形が混在しても、全refが初出順・重複排除で算出される。"""
    answer = "[[2]] 続いてグループ形 [[1], [2]] さらに [[3]]"

    draft = _finalize(
        answer,
        evidence=[_evidence("1"), _evidence("2"), _evidence("3")],
    )

    assert draft.cited_refs == ["2", "1", "3"]


def test_duplicate_ref_within_a_single_group_is_deduplicated() -> None:
    """同一グループ内で同じrefが2回出ても重複排除される。"""
    answer = "同じ根拠を並べます。[[1], [1]]"

    draft = _finalize(answer, evidence=[_evidence("1")])

    assert draft.cited_refs == ["1"]


def test_rejects_answer_when_group_marker_contains_ref_unknown_to_evidence() -> None:
    """Invariants: グループ内に evidence に存在しない ref が1つでもあれば、
    グループ単位で握り潰さずdraftを不正として弾く。"""
    with pytest.raises(EvidenceAnswerDraftInvalidError, match=r"\[\[2\]\]"):
        _finalize("[[1], [2]]", evidence=[_evidence("1")])


def test_group_marker_only_answer_is_not_treated_as_marker_absent() -> None:
    """本文のmarkerがグループ形だけでも「evidenceがあるのにmarker0件」判定に
    ならず、正常にcited_refsが算出される。"""
    answer = "根拠はグループ形だけで示します。[[1], [2]]"

    draft = _finalize(answer, evidence=[_evidence("1"), _evidence("2")])

    assert draft.cited_refs == ["1", "2"]


def test_inner_comma_form_is_not_recognized_as_a_marker() -> None:
    """設計判断3: 内側カンマ形 [[1, 2]] はネスト配列リテラルとの衝突を避けるため
    受理しない。evidenceがあるのにmarker0件として弾かれる形で固定する。"""
    with pytest.raises(EvidenceAnswerDraftInvalidError):
        _finalize(
            "行列のようです。[[1, 2]]",
            evidence=[_evidence("1"), _evidence("2")],
        )
