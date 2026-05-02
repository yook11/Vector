"""ArticleInput VO の制約テスト。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.insights.briefing.domain.article import ArticleInput


class TestArticleInput:
    def test_min_constraints(self) -> None:
        with pytest.raises(ValidationError):
            ArticleInput(id=0, title_ja="t", summary_ja="s")
        with pytest.raises(ValidationError):
            ArticleInput(id=1, title_ja="", summary_ja="s")
        with pytest.raises(ValidationError):
            ArticleInput(id=1, title_ja="t", summary_ja="")

    def test_frozen(self) -> None:
        a = ArticleInput(id=1, title_ja="t", summary_ja="s")
        with pytest.raises(ValidationError):
            a.id = 2  # type: ignore[misc]
