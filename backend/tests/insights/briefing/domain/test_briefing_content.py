"""WeeklyBriefingContent / BriefingChapter / KeyArticle / WatchPoint の検証テスト。

不変条件の正本 (一次防御):
- summary / chapters / key_articles / watch_points の件数・文字列長 (F10 構造防御)
- chapters は最低 1 章 (本文を章立てで構造化する)
- key_articles[].article_id の重複拒否 (UI の React key 一意性)
- key_articles[].article_id ⊆ input_ids (ハルシネーション検出、context 付き)
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.insights.briefing.domain.briefing import (
    MAX_BRIEFING_SUMMARY_LEN,
    MAX_CHAPTER_BODY_LEN,
    MAX_CHAPTER_HEADING_LEN,
    MAX_CHAPTERS_PER_BRIEFING,
    MAX_KEY_ARTICLE_SIGNIFICANCE_LEN,
    MAX_KEY_ARTICLES_PER_BRIEFING,
    MAX_WATCH_POINT_STATEMENT_LEN,
    MAX_WATCH_POINTS_PER_BRIEFING,
    BriefingChapter,
    KeyArticle,
    WatchPoint,
    WeeklyBriefingContent,
)


def _content(**overrides: object) -> dict[str, object]:
    """正常系の最小 payload。各テストで壊したい field だけ上書きする。"""
    base: dict[str, object] = {
        "headline": "今週は AI",
        "summary": "今週の総括リード",
        "chapters": [{"heading": "資金とインフラ", "body": "章本文"}],
        "key_articles": [{"article_id": 1, "significance": "なぜ重要か"}],
        "watch_points": [{"statement": "今後どこを見るべきか"}],
    }
    base.update(overrides)
    return base


class TestBriefingChapter:
    def test_heading_min_length_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            BriefingChapter(heading="", body="章本文")

    def test_body_min_length_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            BriefingChapter(heading="見出し", body="")


class TestKeyArticle:
    def test_significance_min_length_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            KeyArticle(article_id=1, significance="")


class TestWatchPoint:
    def test_statement_min_length_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            WatchPoint(statement="")


class TestWeeklyBriefingContentNormalPath:
    def test_accepts_minimal_valid_content(self) -> None:
        content = WeeklyBriefingContent.model_validate(_content())
        assert content.summary == "今週の総括リード"
        assert content.chapters[0].heading == "資金とインフラ"
        assert content.chapters[0].body == "章本文"
        assert content.key_articles[0].article_id == 1
        assert content.key_articles[0].significance == "なぜ重要か"
        assert content.watch_points[0].statement == "今後どこを見るべきか"

    def test_summary_min_length_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.model_validate(_content(summary=""))

    def test_rejects_oversize_summary(self) -> None:
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.model_validate(
                _content(summary="x" * (MAX_BRIEFING_SUMMARY_LEN + 1))
            )


class TestChaptersInvariants:
    def test_rejects_empty_chapters(self) -> None:
        """章は最低 1 つ必要 (本文を章立てで構造化する)。"""
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.model_validate(_content(chapters=[]))

    def test_rejects_more_than_max_chapters(self) -> None:
        """章数の上限ガード超 (LLM 暴走疑い) を弾く。"""
        too_many = [
            {"heading": f"h{i}", "body": f"b{i}"}
            for i in range(MAX_CHAPTERS_PER_BRIEFING + 1)
        ]
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.model_validate(_content(chapters=too_many))

    def test_rejects_oversize_heading(self) -> None:
        oversize = [{"heading": "x" * (MAX_CHAPTER_HEADING_LEN + 1), "body": "b"}]
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.model_validate(_content(chapters=oversize))

    def test_rejects_oversize_body(self) -> None:
        oversize = [{"heading": "h", "body": "x" * (MAX_CHAPTER_BODY_LEN + 1)}]
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.model_validate(_content(chapters=oversize))


class TestKeyArticlesInvariants:
    def test_rejects_empty_key_articles(self) -> None:
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.model_validate(_content(key_articles=[]))

    def test_rejects_abnormal_key_article_count(self) -> None:
        """F10 異常検知ライン超 (injection / 暴走疑い) を弾く。

        editorial 上限 (プロンプトの「最大 5 件」) ではなく、structural な
        ``MAX_KEY_ARTICLES_PER_BRIEFING`` 超を境界とする。
        """
        too_many = [
            {"article_id": i, "significance": f"s{i}"}
            for i in range(MAX_KEY_ARTICLES_PER_BRIEFING + 1)
        ]
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.model_validate(_content(key_articles=too_many))

    def test_rejects_duplicate_key_article_ids(self) -> None:
        """同一 article_id の重複は件数と独立に常時拒否する。"""
        dup = [
            {"article_id": 7, "significance": "a"},
            {"article_id": 7, "significance": "b"},
        ]
        with pytest.raises(ValidationError, match="duplicate"):
            WeeklyBriefingContent.model_validate(_content(key_articles=dup))

    def test_rejects_oversize_significance(self) -> None:
        oversize = [
            {
                "article_id": 1,
                "significance": "x" * (MAX_KEY_ARTICLE_SIGNIFICANCE_LEN + 1),
            }
        ]
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.model_validate(_content(key_articles=oversize))


class TestWatchPointsInvariants:
    def test_rejects_empty_watch_points(self) -> None:
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.model_validate(_content(watch_points=[]))

    def test_rejects_more_than_max_watch_points(self) -> None:
        too_many = [
            {"statement": f"w{i}"} for i in range(MAX_WATCH_POINTS_PER_BRIEFING + 1)
        ]
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.model_validate(_content(watch_points=too_many))

    def test_rejects_oversize_statement(self) -> None:
        oversize = [{"statement": "x" * (MAX_WATCH_POINT_STATEMENT_LEN + 1)}]
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.model_validate(_content(watch_points=oversize))


class TestArticleIdSubsetCheck:
    def test_skips_subset_check_without_context(self) -> None:
        """context 未指定のときは subset 検証をスキップする (テスト/CLI 経路許容)。"""
        content = WeeklyBriefingContent.model_validate(
            _content(key_articles=[{"article_id": 42, "significance": "s"}])
        )
        assert content.key_articles[0].article_id == 42

    def test_subset_check_passes_when_all_ids_in_input(self) -> None:
        data = _content(
            key_articles=[
                {"article_id": 1, "significance": "a"},
                {"article_id": 2, "significance": "b"},
            ]
        )
        result = WeeklyBriefingContent.model_validate(
            data, context={"input_ids": {1, 2, 3}}
        )
        assert [ka.article_id for ka in result.key_articles] == [1, 2]

    def test_rejects_key_article_id_not_in_input_ids(self) -> None:
        """LLM が捏造した article_id を構造的に弾く。"""
        data = _content(
            key_articles=[
                {"article_id": 1, "significance": "a"},
                {"article_id": 999, "significance": "b"},
            ]
        )
        with pytest.raises(ValidationError, match="999"):
            WeeklyBriefingContent.model_validate(data, context={"input_ids": {1, 2, 3}})


def _valid_payload(
    *,
    headline: str = "今週のまとめ",
    article_id: int = 10,
) -> str:
    """from_llm_payload テスト用の最小 JSON 文字列。"""
    import json

    return json.dumps(
        {
            "headline": headline,
            "summary": "今週の総括",
            "chapters": [{"heading": "見出し", "body": "本文"}],
            "key_articles": [{"article_id": article_id, "significance": "重要な理由"}],
            "watch_points": [{"statement": "観察すべき論点"}],
        }
    )


class TestFromLlmPayload:
    """WeeklyBriefingContent.from_llm_payload の不変条件。"""

    def test_valid_json_with_matching_input_ids_returns_vo(self) -> None:
        """正常 JSON + input_ids ⊇ key_articles で VO が返る。"""
        content = WeeklyBriefingContent.from_llm_payload(
            _valid_payload(article_id=10), input_ids={10, 20}
        )
        assert content.key_articles[0].article_id == 10
        assert content.headline == "今週のまとめ"

    def test_input_ids_outside_key_articles_raises_validation_error(self) -> None:
        """key_articles の article_id が input_ids 外のとき ValidationError。

        from_llm_payload を通ると context が必ず渡されるため、
        捏造 article_id は必ず弾かれる (context 渡し忘れスキップが起きない)。
        """
        with pytest.raises(ValidationError, match="999"):
            WeeklyBriefingContent.from_llm_payload(
                _valid_payload(article_id=999), input_ids={1, 2}
            )

    def test_context_is_always_passed_so_hallucination_check_cannot_be_skipped(
        self,
    ) -> None:
        """from_llm_payload を通れば input_ids 検証が必ず実行される。

        context を渡さない model_validate では捏造 id がスルーされるが
        (test_skips_subset_check_without_context で確認)、from_llm_payload
        経由では input_ids が必須引数のため同じスキップが構造的に起きない。
        """
        # id=42 を input_ids に含めなければ ValidationError になることを確認
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.from_llm_payload(
                _valid_payload(article_id=42), input_ids={1}
            )
        # id=42 を含めれば通ることで「from_llm_payload が検証を実行している」を保証
        result = WeeklyBriefingContent.from_llm_payload(
            _valid_payload(article_id=42), input_ids={42}
        )
        assert result.key_articles[0].article_id == 42

    def test_invalid_json_raises_validation_error(self) -> None:
        """不正 JSON は ValidationError。"""
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.from_llm_payload("not-json", input_ids={1})

    def test_missing_required_field_raises_validation_error(self) -> None:
        """必須フィールド欠落は ValidationError。"""
        import json

        payload = json.dumps(
            {
                # headline を意図的に欠落
                "summary": "今週の総括",
                "chapters": [{"heading": "見出し", "body": "本文"}],
                "key_articles": [{"article_id": 1, "significance": "s"}],
                "watch_points": [{"statement": "w"}],
            }
        )
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.from_llm_payload(payload, input_ids={1})

    def test_empty_key_articles_raises_validation_error(self) -> None:
        """key_articles が空リストは min_length=1 で ValidationError。"""
        import json

        payload = json.dumps(
            {
                "headline": "h",
                "summary": "s",
                "chapters": [{"heading": "見出し", "body": "本文"}],
                "key_articles": [],
                "watch_points": [{"statement": "w"}],
            }
        )
        with pytest.raises(ValidationError):
            WeeklyBriefingContent.from_llm_payload(payload, input_ids=set())
