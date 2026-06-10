"""ArticleService の純関数テスト — ``extract_key_point_contents`` の防御分岐。

JSONB key_points は本番に旧形 (NULL) や AI 由来の不定形が混じりうるため、
content だけを安全に取り出す純関数の境界を固定する。API 契約 (keyPoints の
順序保持 / mentions 非公開) は ``tests/test_routers/test_articles.py`` が所有する。
"""

from __future__ import annotations

from app.services.articles import extract_key_point_contents


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
