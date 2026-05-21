"""ReadyForCuration (Stage 3 precondition 型) のドメインユニットテスト。

PR3 案 3 化: ``try_advance_from`` は Repository の ``try_load_for_curation``
への thin delegate になったため、本ファイルでは:

- ``try_advance_from`` が Protocol の ``try_load_for_curation`` をそのまま
  呼び返し値を返すこと (thin delegate)
- ``BaseModel(frozen=True)`` の不変性 + ``Field`` 制約 (構造保証)

を検証する。precondition 判定の中身 (Article fetch / signal/noise exists 判定 /
oversize check) は Repository 単独テスト (``test_extraction_repository.py``) で
カバーする。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.analysis.curation.domain.ready import (
    CurationTrigger,
    ReadyForCuration,
)


def _ready_repo_returning(ready: ReadyForCuration | None) -> AsyncMock:
    """``CurationPreconditionProtocol`` の thin mock を返す。"""
    repo = AsyncMock()
    repo.try_load_for_curation = AsyncMock(return_value=ready)
    return repo


# ---------------------------------------------------------------------------
# try_advance_from — thin delegate (Repository.try_load_for_curation)
# ---------------------------------------------------------------------------


class TestTryAdvanceFromThinDelegate:
    @pytest.mark.asyncio
    async def test_returns_ready_when_repo_returns_ready(self) -> None:
        """repo が Ready を返したら、try_advance_from もそのまま返す。"""
        expected = ReadyForCuration(
            article_id=42,
            original_title="Quantum Breakthrough",
            original_content="Article body" * 10,
        )
        repo = _ready_repo_returning(expected)

        ready = await ReadyForCuration.try_advance_from(article_id=42, repo=repo)

        assert ready is expected
        repo.try_load_for_curation.assert_awaited_once_with(42)

    @pytest.mark.asyncio
    async def test_returns_none_when_repo_returns_none(self) -> None:
        """repo が None を返したら、try_advance_from も None を返す。"""
        repo = _ready_repo_returning(None)

        ready = await ReadyForCuration.try_advance_from(article_id=99, repo=repo)

        assert ready is None
        repo.try_load_for_curation.assert_awaited_once_with(99)


# ---------------------------------------------------------------------------
# Ready 型の Field 制約 (Pydantic 構造保証 — 直接構築の防御層)
# ---------------------------------------------------------------------------


class TestReadyForCurationFieldConstraints:
    def test_rejects_empty_original_title(self) -> None:
        """空文字 original_title は Field(min_length=1) が拒否する。"""
        with pytest.raises(ValidationError):
            ReadyForCuration(article_id=1, original_title="", original_content="x")

    def test_rejects_empty_original_content(self) -> None:
        """空文字 original_content は Field(min_length=1) が拒否する。"""
        with pytest.raises(ValidationError):
            ReadyForCuration(article_id=1, original_title="t", original_content="")

    def test_rejects_oversized_original_content(self) -> None:
        """直接構築でも MAX_CONTENT_LENGTH 超過は ValidationError (防御層)。"""
        oversized = "x" * (ReadyForCuration.MAX_CONTENT_LENGTH + 1)
        with pytest.raises(ValidationError):
            ReadyForCuration(
                article_id=1, original_title="t", original_content=oversized
            )

    def test_rejects_non_positive_article_id(self) -> None:
        """article_id <= 0 は Field(gt=0) が拒否する。"""
        with pytest.raises(ValidationError):
            ReadyForCuration(article_id=0, original_title="t", original_content="x")
        with pytest.raises(ValidationError):
            ReadyForCuration(article_id=-1, original_title="t", original_content="x")

    def test_is_frozen(self) -> None:
        """frozen=True のため field 書き換えは ValidationError。"""
        ready = ReadyForCuration(article_id=1, original_title="t", original_content="x")
        with pytest.raises(ValidationError):
            ready.article_id = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CurationTrigger — 軽量 ID キャリア (kiq message 用)
# ---------------------------------------------------------------------------


class TestCurationTrigger:
    def test_carries_article_id_only(self) -> None:
        """article_id のみを保持する軽量 BaseModel。"""
        trigger = CurationTrigger(article_id=42)
        assert trigger.article_id == 42

    def test_rejects_non_positive_article_id(self) -> None:
        """article_id <= 0 は Field(gt=0) が拒否する。"""
        with pytest.raises(ValidationError):
            CurationTrigger(article_id=0)
        with pytest.raises(ValidationError):
            CurationTrigger(article_id=-1)

    def test_is_frozen(self) -> None:
        """frozen=True のため field 書き換えは ValidationError。"""
        trigger = CurationTrigger(article_id=1)
        with pytest.raises(ValidationError):
            trigger.article_id = 999  # type: ignore[misc]
