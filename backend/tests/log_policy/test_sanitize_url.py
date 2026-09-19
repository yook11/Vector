"""URL userinfo の置換と、scheme / 接続先を残す境界。"""

import pytest

from app.log_policy.sanitize import sanitize_url_userinfo

pytestmark = pytest.mark.unit


def test_userinfo_with_password_is_hidden_and_endpoint_kept() -> None:
    """user:password 形式の userinfo を伏せ、scheme と接続先を残す。"""
    text = "failed https://vector:s3cr3tp4ss@db.example.invalid:5432/vector"
    assert sanitize_url_userinfo(text) == (
        "failed https://***@db.example.invalid:5432/vector"
    )


def test_userinfo_without_password_is_hidden_and_endpoint_kept() -> None:
    """パスワードのない userinfo も伏せ、scheme と接続先を残す。"""
    text = "failed https://vector@db.example.invalid/path"
    assert sanitize_url_userinfo(text) == "failed https://***@db.example.invalid/path"


def test_uppercase_scheme_userinfo_is_hidden_and_endpoint_kept() -> None:
    """大文字の scheme でも userinfo を伏せ、接続先を残す。"""
    text = "failed HTTP://vector:s3cr3tp4ss@db.example.invalid/path"
    assert sanitize_url_userinfo(text) == ("failed HTTP://***@db.example.invalid/path")


@pytest.mark.parametrize("prefix", ["-", "1", "1.", "+."])
def test_adjacent_nonletter_prefix_does_not_expose_userinfo(prefix: str) -> None:
    """自由文中で数字や記号が隣接してもURLの認証部分を伏せる。"""
    text = f"failed {prefix}https://user:synthetic@host/path"
    assert sanitize_url_userinfo(text) == f"failed {prefix}https://***@host/path"


def test_scheme_without_any_letter_is_not_treated_as_url() -> None:
    """英字を含まないscheme候補は従来どおり置換しない。"""
    text = "failed 123+.-://user:synthetic@host"
    assert sanitize_url_userinfo(text) == text


@pytest.mark.parametrize("letter", ["İ", "ı", "ſ", "K"])
def test_scheme_keeps_previous_ignorecase_character_coverage(letter: str) -> None:
    """以前のIGNORECASEで認識した文字を含むschemeも秘匿範囲から外さない。"""
    text = f"failed {letter}://user:synthetic@host"
    assert sanitize_url_userinfo(text) == f"failed {letter}://***@host"


def test_multiple_prefixed_urls_are_protected_independently() -> None:
    """先のURLを置換しても後続のURLのscheme境界を巻き込まない。"""
    text = "-https://user:synthetic@host 1postgresql+asyncpg://user:synthetic@db"
    assert (
        sanitize_url_userinfo(text) == "-https://***@host 1postgresql+asyncpg://***@db"
    )


def test_long_public_url_without_userinfo_is_preserved() -> None:
    """長い公開URLのpathを認証情報として置換しない。"""
    text = "https://example.invalid/" + "x" * 32768
    assert sanitize_url_userinfo(text) == text
