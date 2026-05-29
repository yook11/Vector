"""``AnalyzableArticleDefect`` の audit outcome_code 契約テスト。

ready の ``test_ready_build_blocked_code_contract.py`` と同形: enum.value が
そのまま audit に焼かれる自己記述コードであることを構造的に保証する。fallback の
``UNMAPPED_VALIDATION_ERROR`` のみ value が name と語順違い (写像漏れを表す造語) の
ため一律規約から除外し、値を個別 assert する。
"""

from __future__ import annotations

import pytest

from app.collection.domain.analyzable_article import AnalyzableArticleDefect


@pytest.mark.parametrize(
    "member",
    [m for m in AnalyzableArticleDefect if m is not m.UNMAPPED_VALIDATION_ERROR],
)
def test_defect_code_value_is_audit_outcome_code(
    member: AnalyzableArticleDefect,
) -> None:
    assert member.value == f"analyzable_article_{member.name.lower()}"


def test_unmapped_fallback_value_is_stable() -> None:
    """写像漏れ fallback は規約外の固定値 (語順が逆の造語) で安定する。"""
    assert (
        AnalyzableArticleDefect.UNMAPPED_VALIDATION_ERROR.value
        == "analyzable_article_validation_error_unmapped"
    )
