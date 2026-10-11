"""DBを使わず、既知の記事の不備コードが監査で使う命名規約に従うことを確認する。"""

from __future__ import annotations

import pytest

from app.collection.domain.analyzable_article import AnalyzableArticleDefect


@pytest.mark.parametrize(
    "member",
    list(AnalyzableArticleDefect),
)
def test_defect_code_value_is_audit_outcome_code(
    member: AnalyzableArticleDefect,
) -> None:
    """既知の不備コードは記事の構築条件を示す名前になる。"""
    assert member.value == f"analyzable_article_{member.name.lower()}"
