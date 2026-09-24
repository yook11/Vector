"""JWT形式の文字列を種別付きの表記へ置き換える境界。"""

import pytest

from app.log_policy.leak_prevention import redact_jwts

pytestmark = pytest.mark.unit


def test_jwt_is_replaced_with_surrounding_text_kept() -> None:
    """JWTの3区画をまとめて種別付きの表記に置き換え、周囲の原因文を残す。"""
    # 合成値を分割し、秘密検出ツールの規則に一致させない。
    text = (
        "got eyJhbGciOiJ"
        + "IUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4 upstream"
    )
    assert redact_jwts(text) == "got [redacted:jwt] upstream"


def test_jwt_rule_accepts_base64url_characters_without_decoding() -> None:
    """署名検証やデコードをせず、形式に一致する区画をハイフン・下線ごと置き換える。"""
    assert redact_jwts("got eyJabc_-.eyJdef-_.signature_- failed") == (
        "got [redacted:jwt] failed"
    )


def test_jwt_rule_replaces_every_occurrence() -> None:
    """同じ文中に複数のJWTがあっても認証値を残さない。"""
    text = "first eyJabc.eyJdef.signature second eyJghi.eyJjkl.other"
    assert redact_jwts(text) == "first [redacted:jwt] second [redacted:jwt]"


def test_jwt_rule_preserves_normal_dotted_text() -> None:
    """ドット区切りというだけでホスト名やバージョンを置換しない。"""
    text = "host api.example.invalid version 1.2.3 failed"
    assert redact_jwts(text) == text


def test_jwt_rule_preserves_two_segment_value() -> None:
    """署名区画のない2区画はJWT形式として置換しない。"""
    text = "got eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0 failed"
    assert redact_jwts(text) == text


def test_jwt_rule_preserves_padded_base64_value() -> None:
    """区画内にパディングの=がある値はbase64url形式として置換しない。"""
    text = "got eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0=.signature failed"
    assert redact_jwts(text) == text


def test_jwt_embedded_in_base64url_characters_is_still_hidden() -> None:
    """headerの前に同じ文字集合の接頭辞があっても、eyJの位置から置き換える。"""
    text = "prefixeyJabc.eyJdef.signature"
    assert redact_jwts(text) == "prefix[redacted:jwt]"


@pytest.mark.parametrize("text", ["eyJ.eyJabc.signature", "eyJabc.eyJ.signature"])
def test_jwt_requires_content_after_each_header_prefix(text: str) -> None:
    """先頭2区画にはeyJの後に少なくとも1文字必要という境界を維持する。"""
    assert redact_jwts(text) == text


def test_invalid_earlier_segments_do_not_hide_a_later_jwt() -> None:
    """不成立の候補の後ろから始まるJWTも検出する。"""
    text = "invalid.eyJabc.eyJdef.signature"
    assert redact_jwts(text) == "invalid.[redacted:jwt]"


def test_adjacent_jwts_do_not_reuse_consumed_segments() -> None:
    """ドットで隣接するJWTを重複なく順に置き換える。"""
    text = "eyJabc.eyJdef.sig.eyJghi.eyJjkl.other"
    assert redact_jwts(text) == "[redacted:jwt].[redacted:jwt]"


def test_empty_signature_is_not_hidden() -> None:
    """署名区画が空の候補は置換しない。"""
    text = "eyJabc.eyJdef. failed"
    assert redact_jwts(text) == text


def test_repeated_prefix_without_segment_delimiters_is_preserved() -> None:
    """長いeyJの繰り返しだけではJWTとして置換しない。"""
    text = "eyJ" * 10923
    assert redact_jwts(text) == text


def test_long_two_segment_candidate_is_preserved() -> None:
    """長い2区画候補でも署名がなければ置換しない。"""
    text = "eyJ" * 5461 + "." + "eyJ" * 5461
    assert redact_jwts(text) == text
