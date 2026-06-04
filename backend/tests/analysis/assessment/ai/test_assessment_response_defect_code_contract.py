"""3 検知場所が所有する response defect enum の audit ``outcome_code`` 契約。

``AssessmentResponseInvalidError`` は marker 1 つで分類と recoverable 性だけを担い、
「なぜ失敗したか」は失敗を検知した場所が所有する defect enum の値が運ぶ:

- ``parse.py`` → ``AssessmentResponseDefect`` (内容の schema 違反、provider 非依存)
- ``gemini.py`` → ``GeminiResponseDefect`` (envelope 契約違反)
- ``deepseek.py`` → ``DeepSeekResponseDefect`` (envelope 契約違反)

完成段 ``test_analyzable_article_defect_code_contract.py`` と同形: enum.value が
そのまま audit に焼かれる自己記述コードであることを構造的に保証する。各 enum は
写像漏れ fallback を持たない (各 raise 点で明示 defect を投げる) ため、全 member を
一律 parametrize して命名規約を固定する。
"""

from __future__ import annotations

import pytest

from app.analysis.assessment.ai.deepseek import DeepSeekResponseDefect
from app.analysis.assessment.ai.gemini import GeminiResponseDefect
from app.analysis.assessment.ai.parse import AssessmentResponseDefect


@pytest.mark.parametrize("member", list(AssessmentResponseDefect))
def test_parse_defect_value_follows_namespace(
    member: AssessmentResponseDefect,
) -> None:
    """parse 所有 defect は ``assessment_response_{name}`` (provider 非依存)。"""
    assert member.value == f"assessment_response_{member.name.lower()}"


@pytest.mark.parametrize("member", list(GeminiResponseDefect))
def test_gemini_defect_value_follows_namespace(
    member: GeminiResponseDefect,
) -> None:
    """gemini 所有 defect は provider 名入り ``assessment_response_gemini_{name}``。"""
    assert member.value == f"assessment_response_gemini_{member.name.lower()}"


@pytest.mark.parametrize("member", list(DeepSeekResponseDefect))
def test_deepseek_defect_value_follows_namespace(
    member: DeepSeekResponseDefect,
) -> None:
    """deepseek 所有 defect は ``assessment_response_deepseek_{name}``。"""
    assert member.value == f"assessment_response_deepseek_{member.name.lower()}"


def test_defect_values_are_unique_across_sites() -> None:
    """3 site の defect 値は audit ``outcome_code`` として衝突しない。

    marker が 1 つでも outcome_code 空間は単一なので、検知場所をまたいで一意で
    あることが不変条件 (provider 名 prefix がこの分離を担保する)。
    """
    all_values = [
        *(m.value for m in AssessmentResponseDefect),
        *(m.value for m in GeminiResponseDefect),
        *(m.value for m in DeepSeekResponseDefect),
    ]
    assert len(all_values) == len(set(all_values))
