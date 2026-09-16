"""ReadyForEmbedding (Stage 5 precondition 型) のドメインユニットテスト。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.analysis.embedding.domain.ready import (
    EmbeddingReadyBuildFacts,
    EmbeddingReadyBuildRejectionReason,
    ReadyForEmbedding,
)


def _facts(
    *,
    has_embedding: bool = False,
    analyzable_article_id: int = 42,
    summary: str = "分析要約",
    key_points: object = None,
) -> EmbeddingReadyBuildFacts:
    return EmbeddingReadyBuildFacts(
        analyzable_article_id=analyzable_article_id,
        has_embedding=has_embedding,
        summary=summary,
        key_points=key_points,
    )


class TestEmbeddingInput:
    def test_builds_ready_from_facts(self) -> None:
        facts = _facts(
            key_points=[
                {
                    "content": "OpenAIが新モデルを発表。",
                    "mentions": [
                        {"surface": "OpenAI", "type": "company"},
                        {"surface": "GPT-5", "type": "product"},
                    ],
                },
                {
                    "content": "NVIDIAがBlackwell出荷を拡大。",
                    "mentions": [
                        {"surface": "NVIDIA", "type": "company"},
                        {"surface": "Blackwell", "type": "product"},
                        {"surface": "nvidia", "type": "company"},
                    ],
                },
            ],
        )

        ready, analyzable_article_id = ReadyForEmbedding.from_facts(
            analyzed_article_id=100, facts=facts
        )

        assert ready == ReadyForEmbedding(
            analyzed_article_id=100,
            text_for_embedding=(
                "分析要約\n\n"
                "OpenAIが新モデルを発表。\n"
                "NVIDIAがBlackwell出荷を拡大。\n\n"
                "OpenAI, GPT-5, NVIDIA, Blackwell"
            ),
        )
        assert analyzable_article_id == 42
        assert "分析タイトル" not in ready.text_for_embedding
        assert "company" not in ready.text_for_embedding
        assert "product" not in ready.text_for_embedding
        assert "要約:" not in ready.text_for_embedding
        assert "重要ポイント:" not in ready.text_for_embedding
        assert "登場固有名:" not in ready.text_for_embedding

    def test_uses_article_id_from_facts(self) -> None:
        facts = _facts(analyzable_article_id=55)

        _ready, analyzable_article_id = ReadyForEmbedding.from_facts(
            analyzed_article_id=100, facts=facts
        )

        assert analyzable_article_id == 55

    @pytest.mark.parametrize("key_points", [None, [], {"content": "ignored"}])
    def test_builds_summary_only_when_key_points_absent_or_malformed(
        self, key_points: object
    ) -> None:
        facts = _facts(key_points=key_points)

        ready, _ = ReadyForEmbedding.from_facts(analyzed_article_id=100, facts=facts)

        assert ready.text_for_embedding == "分析要約"

    def test_ignores_malformed_key_point_items(self) -> None:
        facts = _facts(
            key_points=[
                {"mentions": [{"surface": "IgnoredCo", "type": "company"}]},
                {"content": 123, "mentions": []},
                {"content": "", "mentions": []},
                "not-a-dict",
            ],
        )

        ready, _ = ReadyForEmbedding.from_facts(analyzed_article_id=100, facts=facts)

        assert ready.text_for_embedding == "分析要約"

    def test_caps_mentions_at_thirty(self) -> None:
        mentions = [
            {"surface": f"Entity {index:02d}", "type": "company"} for index in range(31)
        ]
        facts = _facts(
            key_points=[
                {
                    "content": "多数の固有名が登場した。",
                    "mentions": mentions,
                }
            ]
        )

        ready, _ = ReadyForEmbedding.from_facts(analyzed_article_id=100, facts=facts)

        mention_line = ready.text_for_embedding.split("\n\n")[-1]
        assert mention_line.split(", ") == [
            f"Entity {index:02d}" for index in range(30)
        ]


class TestReadyForEmbeddingFieldConstraints:
    def test_rejects_non_positive_analyzed_article_id(self) -> None:
        with pytest.raises(ValidationError):
            ReadyForEmbedding(analyzed_article_id=0, text_for_embedding="t")
        with pytest.raises(ValidationError):
            ReadyForEmbedding(analyzed_article_id=-1, text_for_embedding="t")

    def test_rejects_empty_text(self) -> None:
        with pytest.raises(ValidationError):
            ReadyForEmbedding(analyzed_article_id=1, text_for_embedding="")

    def test_is_frozen(self) -> None:
        ready = ReadyForEmbedding(analyzed_article_id=1, text_for_embedding="t\ns")
        with pytest.raises(ValidationError):
            ready.analyzed_article_id = 999  # type: ignore[misc]


def test_rejection_reasons_partition_idempotent_skip_from_durable() -> None:
    """生成済みだけを監査対象から除き、欠損と入力不正の理由を記録する。"""
    idempotent = {c for c in EmbeddingReadyBuildRejectionReason if c.is_idempotent_skip}
    durable = {
        c for c in EmbeddingReadyBuildRejectionReason if not c.is_idempotent_skip
    }
    assert idempotent == {EmbeddingReadyBuildRejectionReason.ALREADY_EMBEDDED}
    assert durable == {
        EmbeddingReadyBuildRejectionReason.ANALYZED_ARTICLE_MISSING,
        EmbeddingReadyBuildRejectionReason.INPUT_INVALID,
    }


class TestFromFacts:
    def test_builds_input_and_uses_fact_article_id(self) -> None:
        """取得済みの事実から本文と監査IDを構築する。"""
        ready, article_id = ReadyForEmbedding.from_facts(
            1,
            _facts(summary="summary", key_points=[{"content": "point"}]),
        )
        assert ready.text_for_embedding == "summary\n\npoint"
        assert article_id == 42

    @pytest.mark.parametrize(
        ("facts", "code"),
        [
            (None, EmbeddingReadyBuildRejectionReason.ANALYZED_ARTICLE_MISSING),
            (
                _facts(has_embedding=True),
                EmbeddingReadyBuildRejectionReason.ALREADY_EMBEDDED,
            ),
        ],
    )
    def test_preserves_rejection_reasons(self, facts, code) -> None:
        """取得済みの事実でも不存在と生成済みの理由を保持する。"""
        rejected = ReadyForEmbedding.from_facts(1, facts)
        assert rejected.reason is code

    @pytest.mark.parametrize(
        "analyzed_article_id, summary", [(1, ""), (0, "summary"), (-1, "summary")]
    )
    def test_rejects_invalid_input_without_io(
        self, analyzed_article_id, summary
    ) -> None:
        """本文の入力検証を副作用なしで実行する。"""
        rejected = ReadyForEmbedding.from_facts(
            analyzed_article_id, _facts(summary=summary)
        )
        assert rejected.reason is EmbeddingReadyBuildRejectionReason.INPUT_INVALID
        assert rejected.analyzable_article_id == _facts().analyzable_article_id
        assert rejected.reason.value == "embedding_ready_build_blocked_input_invalid"
