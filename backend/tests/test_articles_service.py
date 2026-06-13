"""ArticleService の純関数テスト — ``extract_key_point_contents`` の防御分岐
および ``build_brief`` の ArticleBrief 契約。

JSONB key_points は本番に旧形 (NULL) や AI 由来の不定形が混じりうるため、
content だけを安全に取り出す純関数の境界を固定する。API 契約 (keyPoints の
順序保持 / mentions 非公開) は ``tests/test_routers/test_articles.py`` が所有する。

build_brief の不変条件:
- key_points が非空なら summary_preview は None
- key_points が空なら summary_preview は非 None かつ ≤300 字
- key_points は最大3件・各要素 ≤250 字
- summary_preview フィールドは常にシリアライズ出力に存在する(required・nullable)
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.articles import build_brief, extract_key_point_contents

# ---------------------------------------------------------------------------
# in-memory fixture helpers
# ---------------------------------------------------------------------------


def _make_analysis(
    *,
    summary: str = "テスト要約",
    key_points: list[dict] | None = None,
) -> SimpleNamespace:
    """build_brief が読む属性を持つ transient オブジェクトを返す。

    SQLAlchemy session が不要な pure unit 用。build_brief は relationship を
    attribute アクセスで辿るだけなので SimpleNamespace で代替できる。
    """
    source = SimpleNamespace(name="TechCrunch", attribution_label=None)
    article = SimpleNamespace(
        news_source=source,
        published_at=None,
    )
    curation = SimpleNamespace(analyzable_article=article)
    category = SimpleNamespace(slug="ai", name="AI")
    return SimpleNamespace(
        id=1,
        translated_title="テスト記事",
        summary=summary,
        key_points=key_points,
        curation=curation,
        category=category,
    )


def _kp(content: str) -> dict:
    """テスト用 key_point dict を作る。"""
    return {"content": content, "mentions": []}


def test_none_returns_empty_list() -> None:
    # 旧行 (key_points IS NULL) は空配列に畳む。
    assert extract_key_point_contents(None) == []


def test_empty_list_returns_empty_list() -> None:
    assert extract_key_point_contents([]) == []


def test_extracts_content_in_order() -> None:
    key_points = [
        {"content": "first", "mentions": []},
        {"content": "second", "mentions": [{"surface": "X", "type": "company"}]},
    ]
    assert extract_key_point_contents(key_points) == ["first", "second"]


def test_drops_mentions() -> None:
    # mentions は trends 内部利用、content だけ返す。
    key_points = [
        {"content": "body", "mentions": [{"surface": "X", "type": "company"}]}
    ]
    assert extract_key_point_contents(key_points) == ["body"]


def test_skips_element_missing_content() -> None:
    assert extract_key_point_contents([{"mentions": []}]) == []


def test_skips_non_str_content() -> None:
    assert extract_key_point_contents([{"content": 123, "mentions": []}]) == []


def test_skips_empty_string_content() -> None:
    assert extract_key_point_contents([{"content": "", "mentions": []}]) == []


def test_skips_non_dict_element() -> None:
    assert extract_key_point_contents(["not a dict"]) == []  # type: ignore[list-item]


def test_mixes_valid_and_invalid_elements() -> None:
    key_points = [
        {"content": "keep", "mentions": []},
        {"content": "", "mentions": []},
        {"mentions": []},
        {"content": "also keep", "mentions": []},
    ]
    assert extract_key_point_contents(key_points) == ["keep", "also keep"]


# ---------------------------------------------------------------------------
# build_brief — ArticleBrief 契約テスト (Red-first)
#
# ArticleBrief に key_points / summary_preview が実装されて初めて green になる。
# 判定基準: extract_key_point_contents() 後の normalized 結果(表示可能な content
# 件数)を基準とする。raw JSONB が非空でも全 invalid なら空扱いでフォールバック。
# ---------------------------------------------------------------------------


class TestBuildBriefMutualExclusion:
    """Invariant: extract 後の normalized key_points 非空 ⟺ summary_preview is None。

    raw JSONB の有無でなく、display-ready な content が1件以上あるかで判定する。
    invalid 要素のみの JSONB を持つ行も「実効的に空」として summary_preview を返す。
    """

    def test_key_points_present_gives_summary_preview_none(self) -> None:
        # Invariant 1: normalized key_points が1件以上 → summary_preview は None
        analysis = _make_analysis(key_points=[_kp("AIが台頭")])
        brief = build_brief(analysis)
        assert brief.summary_preview is None  # type: ignore[attr-defined]

    def test_key_points_empty_list_gives_non_null_summary_preview(self) -> None:
        # Invariant 2a: key_points が空配列 → summary_preview は非 None
        analysis = _make_analysis(summary="フォールバック要約", key_points=[])
        brief = build_brief(analysis)
        assert brief.summary_preview is not None  # type: ignore[attr-defined]

    def test_key_points_null_gives_non_null_summary_preview(self) -> None:
        # Invariant 2b: key_points が NULL(旧行) → summary_preview は非 None
        analysis = _make_analysis(summary="旧行フォールバック", key_points=None)
        brief = build_brief(analysis)
        assert brief.summary_preview is not None  # type: ignore[attr-defined]

    def test_all_invalid_key_points_treated_as_empty(self) -> None:
        # 全要素 invalid な raw JSONB は normalized 後 空扱い。空扱いでも
        # summary_preview を返し、key_points=[] かつ None の無言カードを防ぐ。
        analysis = _make_analysis(
            summary="フォールバック要約",
            key_points=[{"mentions": []}, {"content": "", "mentions": []}],
        )
        brief = build_brief(analysis)
        assert brief.key_points == []  # type: ignore[attr-defined]
        assert brief.summary_preview == "フォールバック要約"  # type: ignore[attr-defined]


class TestBuildBriefSummaryPreviewLength:
    """Invariant: summary_preview は最大300字。"""

    def test_summary_preview_truncated_to_300_chars(self) -> None:
        # 301字の summary → 300字以内に収まる。
        long_summary = "あ" * 301
        analysis = _make_analysis(summary=long_summary, key_points=[])
        brief = build_brief(analysis)
        assert brief.summary_preview is not None  # type: ignore[attr-defined]
        assert len(brief.summary_preview) <= 300  # type: ignore[attr-defined]

    def test_summary_exactly_300_chars_not_truncated(self) -> None:
        # 300字はそのまま返す(末尾省略しない)。
        exact_summary = "い" * 300
        analysis = _make_analysis(summary=exact_summary, key_points=[])
        brief = build_brief(analysis)
        assert brief.summary_preview == exact_summary  # type: ignore[attr-defined]


class TestBuildBriefKeyPointsCount:
    """Invariant: build_brief は key_points を最大3件に制限して返す。"""

    def test_four_raw_key_points_truncated_to_three(self) -> None:
        # API contract: build_brief は4件目以降を落とす。
        kps = [_kp(f"point{i}") for i in range(4)]
        analysis = _make_analysis(key_points=kps)
        brief = build_brief(analysis)
        assert len(brief.key_points) == 3  # type: ignore[attr-defined]

    def test_three_key_points_all_returned(self) -> None:
        kps = [_kp(f"point{i}") for i in range(3)]
        analysis = _make_analysis(key_points=kps)
        brief = build_brief(analysis)
        assert len(brief.key_points) == 3  # type: ignore[attr-defined]

    def test_key_points_order_preserved_and_fourth_dropped(self) -> None:
        # 先頭3件の順序が保たれ、4件目が落ちる。
        kps = [_kp("alpha"), _kp("beta"), _kp("gamma"), _kp("delta")]
        analysis = _make_analysis(key_points=kps)
        brief = build_brief(analysis)
        assert brief.key_points == ["alpha", "beta", "gamma"]  # type: ignore[attr-defined]


class TestBuildBriefKeyPointLength:
    """Invariant: key_points の各要素は ≤250字。超過時は末尾を省略記号で詰める。"""

    def test_content_over_250_capped_with_ellipsis(self) -> None:
        # 250字 + "TAIL" → 250字以内かつ末尾が省略記号、TAIL は含まれない。
        long_content = "あ" * 250 + "TAIL"
        analysis = _make_analysis(key_points=[_kp(long_content)])
        brief = build_brief(analysis)
        kp = brief.key_points[0]  # type: ignore[attr-defined]
        assert len(kp) <= 250
        assert kp.endswith("…")
        assert "TAIL" not in kp

    def test_content_exactly_250_not_truncated(self) -> None:
        # 250字はそのまま返す(省略記号を付けない)。
        exact_content = "い" * 250
        analysis = _make_analysis(key_points=[_kp(exact_content)])
        brief = build_brief(analysis)
        assert brief.key_points[0] == exact_content  # type: ignore[attr-defined]


class TestBuildBriefFieldPresence:
    """Invariant: summary_preview と key_points キーは常にシリアライズ出力に存在する。
    また summary(全文)は ArticleBrief から削除される。
    """

    def test_summary_preview_key_present_when_key_points_exist(self) -> None:
        # key_points 非空でも summaryPreview キーが JSON に含まれる(値は null)。
        analysis = _make_analysis(key_points=[_kp("point")])
        serialized = build_brief(analysis).model_dump(by_alias=True)  # type: ignore[union-attr]
        assert "summaryPreview" in serialized
        assert serialized["summaryPreview"] is None

    def test_summary_preview_key_present_when_key_points_empty(self) -> None:
        # key_points が空でも summaryPreview キーが JSON に含まれる(値は非 null)。
        analysis = _make_analysis(summary="要約テキスト", key_points=[])
        serialized = build_brief(analysis).model_dump(by_alias=True)  # type: ignore[union-attr]
        assert "summaryPreview" in serialized
        assert serialized["summaryPreview"] is not None

    def test_summary_full_text_absent_from_serialized_brief(self) -> None:
        # ArticleBrief 契約: summary(全文)フィールドは削除される。
        # keyPoints / summaryPreview だけが存在すること。
        analysis = _make_analysis(key_points=[_kp("point")])
        serialized = build_brief(analysis).model_dump(by_alias=True)  # type: ignore[union-attr]
        assert "summary" not in serialized
        assert "keyPoints" in serialized
        assert "summaryPreview" in serialized
