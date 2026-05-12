"""ReadyForExtraction (Stage C precondition 型) のドメインユニットテスト。

`try_advance_from` の precondition 充足 / 未充足 を Repository protocol mock で
検証する (DB 不要)。BaseModel(frozen=True) の不変性 + Field 制約も確認。

PR1-b で ``ExtractionExistenceProtocol`` 1 つに統合 (``signal_exists_for_article``
/ ``noise_exists_for_article`` の 2 メソッドを持つ)、``try_advance_from`` の
signature から ``noise_repo`` 引数を削除。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.analysis.extraction.domain.ready import ReadyForExtraction


def _make_repo_mock(
    *, signal_exists: bool = False, noise_exists: bool = False
) -> AsyncMock:
    """``ExtractionExistenceProtocol`` の 2 メソッドを持つ mock を返す。"""
    repo = AsyncMock()
    repo.signal_exists_for_article = AsyncMock(return_value=signal_exists)
    repo.noise_exists_for_article = AsyncMock(return_value=noise_exists)
    return repo


# ---------------------------------------------------------------------------
# try_advance_from — precondition 充足 / 未充足
# ---------------------------------------------------------------------------


class TestTryAdvanceFromPreconditionMet:
    @pytest.mark.asyncio
    async def test_returns_ready_when_not_yet_extracted(self) -> None:
        """extraction/noise 未生成 + 本文サイズ妥当なら Ready を返す。"""
        repo = _make_repo_mock(signal_exists=False, noise_exists=False)

        ready = await ReadyForExtraction.try_advance_from(
            article_id=42,
            original_title="Quantum Breakthrough",
            original_content="Article body" * 10,
            extraction_repo=repo,
        )

        assert ready is not None
        assert ready.article_id == 42
        assert ready.original_title == "Quantum Breakthrough"
        assert ready.original_content == "Article body" * 10

    @pytest.mark.asyncio
    async def test_calls_exists_methods_with_article_id(self) -> None:
        """exists 判定は article_id をキーに signal / noise 両方で行う。"""
        repo = _make_repo_mock(signal_exists=False, noise_exists=False)

        await ReadyForExtraction.try_advance_from(
            article_id=777,
            original_title="t",
            original_content="content body",
            extraction_repo=repo,
        )

        repo.signal_exists_for_article.assert_awaited_once_with(777)
        repo.noise_exists_for_article.assert_awaited_once_with(777)


class TestTryAdvanceFromPreconditionNotMet:
    @pytest.mark.asyncio
    async def test_returns_none_when_already_extracted(self) -> None:
        """同 article_id に extraction 既存なら None を返す (業務正常)。"""
        repo = _make_repo_mock(signal_exists=True, noise_exists=False)

        ready = await ReadyForExtraction.try_advance_from(
            article_id=42,
            original_title="t",
            original_content="content",
            extraction_repo=repo,
        )

        assert ready is None

    @pytest.mark.asyncio
    async def test_returns_none_when_already_recorded_as_noise(self) -> None:
        """同 article_id に noise 既存なら None を返す (再処理しない)。"""
        repo = _make_repo_mock(signal_exists=False, noise_exists=True)

        ready = await ReadyForExtraction.try_advance_from(
            article_id=42,
            original_title="t",
            original_content="content",
            extraction_repo=repo,
        )

        assert ready is None

    @pytest.mark.asyncio
    async def test_returns_none_when_content_exceeds_hard_cap(self) -> None:
        """system hard cap (200_000 char) 超過の本文なら None を返す (skip)。"""
        repo = _make_repo_mock(signal_exists=False, noise_exists=False)
        oversized = "x" * (ReadyForExtraction.MAX_CONTENT_LENGTH + 1)

        ready = await ReadyForExtraction.try_advance_from(
            article_id=42,
            original_title="t",
            original_content=oversized,
            extraction_repo=repo,
        )

        assert ready is None

    @pytest.mark.asyncio
    async def test_accepts_content_at_exact_hard_cap(self) -> None:
        """境界値: ちょうど MAX_CONTENT_LENGTH 文字なら advance できる。"""
        repo = _make_repo_mock(signal_exists=False, noise_exists=False)
        boundary = "y" * ReadyForExtraction.MAX_CONTENT_LENGTH

        ready = await ReadyForExtraction.try_advance_from(
            article_id=42,
            original_title="t",
            original_content=boundary,
            extraction_repo=repo,
        )

        assert ready is not None
        assert len(ready.original_content) == ReadyForExtraction.MAX_CONTENT_LENGTH


# ---------------------------------------------------------------------------
# Ready 型の Field 制約 (Pydantic 構造保証)
# ---------------------------------------------------------------------------


class TestReadyForExtractionFieldConstraints:
    def test_rejects_empty_original_title(self) -> None:
        """空文字 original_title は Field(min_length=1) が拒否する。"""
        with pytest.raises(ValidationError):
            ReadyForExtraction(article_id=1, original_title="", original_content="x")

    def test_rejects_empty_original_content(self) -> None:
        """空文字 original_content は Field(min_length=1) が拒否する。"""
        with pytest.raises(ValidationError):
            ReadyForExtraction(article_id=1, original_title="t", original_content="")

    def test_rejects_oversized_original_content(self) -> None:
        """直接構築でも MAX_CONTENT_LENGTH 超過は ValidationError (防御層)。"""
        oversized = "x" * (ReadyForExtraction.MAX_CONTENT_LENGTH + 1)
        with pytest.raises(ValidationError):
            ReadyForExtraction(
                article_id=1, original_title="t", original_content=oversized
            )

    def test_rejects_non_positive_article_id(self) -> None:
        """article_id <= 0 は Field(gt=0) が拒否する。"""
        with pytest.raises(ValidationError):
            ReadyForExtraction(article_id=0, original_title="t", original_content="x")
        with pytest.raises(ValidationError):
            ReadyForExtraction(article_id=-1, original_title="t", original_content="x")

    def test_is_frozen(self) -> None:
        """frozen=True のため field 書き換えは ValidationError。"""
        ready = ReadyForExtraction(
            article_id=1, original_title="t", original_content="x"
        )
        with pytest.raises(ValidationError):
            ready.article_id = 999  # type: ignore[misc]
