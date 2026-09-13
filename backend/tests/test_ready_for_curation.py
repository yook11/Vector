"""ReadyForCuration (Stage 3 precondition 型) のドメインユニットテスト。"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.analysis.curation.domain.ready import (
    CurationReadyBuildFacts,
    CurationReadyBuildRejected,
    CurationReadyBuildRejectionReason,
    ReadyForCuration,
)
from app.queue.messages.curation import CurationTrigger


def _facts(
    *,
    analyzable_article_id: int = 42,
    title: str = "Quantum Breakthrough",
    content: str = "Article body",
    has_signal_curation: bool = False,
    has_noise_curation: bool = False,
) -> CurationReadyBuildFacts:
    return CurationReadyBuildFacts(
        analyzable_article_id=analyzable_article_id,
        original_title=title,
        original_content=content,
        has_signal_curation=has_signal_curation,
        has_noise_curation=has_noise_curation,
    )


def _repo_mock(
    *,
    facts: CurationReadyBuildFacts | None = None,
    missing: bool = False,
) -> AsyncMock:
    repo = AsyncMock()
    repo.load_ready_build_facts = AsyncMock(
        return_value=None if missing else facts or _facts()
    )
    return repo


class TestTryAdvanceFrom:
    @pytest.mark.asyncio
    async def test_builds_ready_from_repository_facts(self) -> None:
        facts = _facts(analyzable_article_id=42, content="Article body" * 10)
        repo = _repo_mock(facts=facts)

        ready = await ReadyForCuration.try_advance_from(
            analyzable_article_id=42, repo=repo
        )

        assert ready == ReadyForCuration(
            analyzable_article_id=42,
            original_title=facts.original_title,
            original_content=facts.original_content,
        )
        repo.load_ready_build_facts.assert_awaited_once_with(42)

    @pytest.mark.asyncio
    async def test_returns_rejection_when_article_missing(self) -> None:
        repo = _repo_mock(missing=True)

        rejected = await ReadyForCuration.try_advance_from(
            analyzable_article_id=99, repo=repo
        )

        assert rejected.reason is CurationReadyBuildRejectionReason.ARTICLE_MISSING
        # 記事不在 → analyzable_article_id は運べない (audit の source_id も空になる)
        assert rejected.analyzable_article_id is None
        repo.load_ready_build_facts.assert_awaited_once_with(99)

    @pytest.mark.asyncio
    async def test_returns_rejection_when_signal_exists(self) -> None:
        repo = _repo_mock(facts=_facts(has_signal_curation=True))

        rejected = await ReadyForCuration.try_advance_from(
            analyzable_article_id=42, repo=repo
        )

        assert rejected.reason is CurationReadyBuildRejectionReason.ALREADY_CURATED
        # analyzable_article_id が拒否値経由で監査まで運ばれる (source_id 補填の根拠)
        assert rejected.analyzable_article_id == 42
        repo.load_ready_build_facts.assert_awaited_once_with(42)

    @pytest.mark.asyncio
    async def test_returns_rejection_when_noise_exists(self) -> None:
        repo = _repo_mock(facts=_facts(has_noise_curation=True))

        rejected = await ReadyForCuration.try_advance_from(
            analyzable_article_id=42, repo=repo
        )

        assert (
            rejected.reason
            is CurationReadyBuildRejectionReason.ALREADY_REJECTED_AS_NOISE
        )
        # analyzable_article_id が拒否値経由で監査まで運ばれる (source_id 補填の根拠)
        assert rejected.analyzable_article_id == 42
        repo.load_ready_build_facts.assert_awaited_once_with(42)

    @pytest.mark.asyncio
    async def test_returns_rejection_when_content_too_large(self) -> None:
        oversized = "x" * (ReadyForCuration.MAX_CONTENT_LENGTH + 1)
        repo = _repo_mock(facts=_facts(content=oversized))

        rejected = await ReadyForCuration.try_advance_from(
            analyzable_article_id=42, repo=repo
        )

        assert rejected.reason is CurationReadyBuildRejectionReason.CONTENT_TOO_LARGE
        # analyzable_article_id が拒否値経由で監査まで運ばれる (source_id 補填の根拠)
        assert rejected.analyzable_article_id == 42
        assert rejected.content_length == len(oversized)
        assert rejected.max_content_length == ReadyForCuration.MAX_CONTENT_LENGTH


class TestReadyForCurationFieldConstraints:
    def test_rejects_empty_original_title(self) -> None:
        with pytest.raises(ValidationError):
            ReadyForCuration(
                analyzable_article_id=1, original_title="", original_content="x"
            )

    def test_rejects_empty_original_content(self) -> None:
        with pytest.raises(ValidationError):
            ReadyForCuration(
                analyzable_article_id=1, original_title="t", original_content=""
            )

    def test_rejects_oversized_original_content(self) -> None:
        oversized = "x" * (ReadyForCuration.MAX_CONTENT_LENGTH + 1)
        with pytest.raises(ValidationError):
            ReadyForCuration(
                analyzable_article_id=1,
                original_title="t",
                original_content=oversized,
            )

    def test_rejects_non_positive_article_id(self) -> None:
        with pytest.raises(ValidationError):
            ReadyForCuration(
                analyzable_article_id=0, original_title="t", original_content="x"
            )
        with pytest.raises(ValidationError):
            ReadyForCuration(
                analyzable_article_id=-1, original_title="t", original_content="x"
            )

    def test_is_frozen(self) -> None:
        ready = ReadyForCuration(
            analyzable_article_id=1, original_title="t", original_content="x"
        )
        with pytest.raises(ValidationError):
            ready.analyzable_article_id = 999  # type: ignore[misc]


class TestCurationTrigger:
    def test_carries_article_id_only(self) -> None:
        trigger = CurationTrigger(analyzable_article_id=42)
        assert trigger.analyzable_article_id == 42

    def test_rejects_non_positive_article_id(self) -> None:
        with pytest.raises(ValidationError):
            CurationTrigger(analyzable_article_id=0)
        with pytest.raises(ValidationError):
            CurationTrigger(analyzable_article_id=-1)


def test_ready_build_blocked_code_partitions_idempotent_skip_from_durable() -> None:
    """ALREADY_* のみ冪等 skip、MISSING/CONTENT_TOO_LARGE は残す恒久的棄却。"""
    idempotent = {c for c in CurationReadyBuildRejectionReason if c.is_idempotent_skip}
    durable = {c for c in CurationReadyBuildRejectionReason if not c.is_idempotent_skip}
    assert idempotent == {
        CurationReadyBuildRejectionReason.ALREADY_CURATED,
        CurationReadyBuildRejectionReason.ALREADY_REJECTED_AS_NOISE,
    }
    assert durable == {
        CurationReadyBuildRejectionReason.ARTICLE_MISSING,
        CurationReadyBuildRejectionReason.CONTENT_TOO_LARGE,
        CurationReadyBuildRejectionReason.INPUT_INVALID,
    }


@pytest.mark.parametrize("length", [1, ReadyForCuration.MAX_CONTENT_LENGTH])
def test_from_facts_accepts_content_boundaries(length):
    """モデルが許す本文長ならReadyを構築する。"""
    result = ReadyForCuration.from_facts(_facts(content="あ" * length))
    assert isinstance(result, ReadyForCuration)
    assert len(result.original_content) == length


@pytest.mark.parametrize(
    "field, value",
    [
        ("original_title", ""),
        ("original_content", ""),
        ("analyzable_article_id", 0),
        ("analyzable_article_id", -1),
    ],
)
def test_from_facts_returns_input_invalid(field, value):
    """モデルの入力制約違反は入力値を持たない拒否結果になる。"""
    facts = replace(_facts(), **{field: value})
    result = ReadyForCuration.from_facts(facts)
    assert result == CurationReadyBuildRejected(
        CurationReadyBuildRejectionReason.INPUT_INVALID,
        analyzable_article_id=facts.analyzable_article_id,
    )


def test_content_limit_rejection_takes_precedence_over_other_input_errors():
    """複数の入力違反があっても本文上限超過の理由と数値だけを保持する。"""
    length = ReadyForCuration.MAX_CONTENT_LENGTH + 1
    result = ReadyForCuration.from_facts(_facts(title="", content="秘" * length))
    assert result == CurationReadyBuildRejected(
        CurationReadyBuildRejectionReason.CONTENT_TOO_LARGE,
        analyzable_article_id=42,
        content_length=length,
        max_content_length=ReadyForCuration.MAX_CONTENT_LENGTH,
    )
    assert "秘" not in repr(result)


@pytest.mark.parametrize(
    "signal, noise, reason",
    [
        (True, True, CurationReadyBuildRejectionReason.ALREADY_CURATED),
        (False, True, CurationReadyBuildRejectionReason.ALREADY_REJECTED_AS_NOISE),
    ],
)
def test_processed_state_precedes_input_validation(signal, noise, reason):
    """処理済みなら入力制約を再評価せず、既存の理由優先順位を維持する。"""
    result = ReadyForCuration.from_facts(
        _facts(
            title="",
            content="x" * (ReadyForCuration.MAX_CONTENT_LENGTH + 1),
            has_signal_curation=signal,
            has_noise_curation=noise,
        )
    )
    assert result == CurationReadyBuildRejected(reason, analyzable_article_id=42)


@pytest.mark.asyncio
async def test_repository_failure_propagates_unchanged():
    """DB取得の失敗をReady拒否へ置き換えない。"""
    original = RuntimeError("repository failure")
    repo = _repo_mock()
    repo.load_ready_build_facts.side_effect = original
    with pytest.raises(RuntimeError) as raised:
        await ReadyForCuration.try_advance_from(analyzable_article_id=42, repo=repo)
    assert raised.value is original


def test_unexpected_model_failure_propagates_unchanged(monkeypatch):
    """モデル構築の想定外例外を入力不正と誤認しない。"""
    original = RuntimeError("model failure")

    def fail(self, **kwargs):
        raise original

    monkeypatch.setattr(ReadyForCuration, "__init__", fail)
    with pytest.raises(RuntimeError) as raised:
        ReadyForCuration.from_facts(_facts())
    assert raised.value is original
