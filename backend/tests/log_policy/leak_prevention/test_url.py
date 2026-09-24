"""URL userinfo を種別付きの表記へ置き換え、scheme / 接続先を残す境界。"""

import pytest

from app.log_policy.leak_prevention import redact_url_userinfo

pytestmark = pytest.mark.unit


def test_userinfo_with_password_is_hidden_and_endpoint_kept() -> None:
    """user:password 形式の userinfo を置き換え、scheme と接続先を残す。"""
    text = "failed https://vector:s3cr3tp4ss@db.example.invalid:5432/vector"
    assert redact_url_userinfo(text) == (
        "failed https://[redacted:url_userinfo]@db.example.invalid:5432/vector"
    )


def test_userinfo_without_password_is_hidden_and_endpoint_kept() -> None:
    """パスワードのない userinfo も置き換え、scheme と接続先を残す。"""
    text = "failed https://vector@db.example.invalid/path"
    assert redact_url_userinfo(text) == (
        "failed https://[redacted:url_userinfo]@db.example.invalid/path"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "postgresql+asyncpg://vector:s3cr3tp4ss@db:5432/vector",
            "postgresql+asyncpg://[redacted:url_userinfo]@db:5432/vector",
            id="postgres_with_driver",
        ),
        pytest.param(
            "rediss://default:hunter2redacted@cache:6380/0",
            "rediss://[redacted:url_userinfo]@cache:6380/0",
            id="redis",
        ),
    ],
)
def test_connection_url_userinfo_is_hidden_and_endpoint_kept(
    text: str, expected: str
) -> None:
    """driver付きPostgreSQLやRedisの接続文字列でも認証部分を置き換え、接続先を残す。"""
    assert redact_url_userinfo(text) == expected


def test_uppercase_scheme_userinfo_is_hidden_and_endpoint_kept() -> None:
    """大文字の scheme でも userinfo を置き換え、接続先を残す。"""
    text = "failed HTTP://vector:s3cr3tp4ss@db.example.invalid/path"
    assert redact_url_userinfo(text) == (
        "failed HTTP://[redacted:url_userinfo]@db.example.invalid/path"
    )


@pytest.mark.parametrize("prefix", ["-", "1", "1.", "+."])
def test_adjacent_nonletter_prefix_does_not_expose_userinfo(prefix: str) -> None:
    """自由文中で数字や記号が隣接してもURLの認証部分を置き換える。"""
    text = f"failed {prefix}https://user:synthetic@host/path"
    assert redact_url_userinfo(text) == (
        f"failed {prefix}https://[redacted:url_userinfo]@host/path"
    )


def test_scheme_without_any_letter_is_not_treated_as_url() -> None:
    """英字を含まないscheme候補は置換しない。"""
    text = "failed 123+.-://user:synthetic@host"
    assert redact_url_userinfo(text) == text


@pytest.mark.parametrize(
    "letter",
    [
        pytest.param("\u0130", id="capital_i_with_dot"),
        pytest.param("\u0131", id="dotless_i"),
        pytest.param("\u017f", id="long_s"),
        pytest.param("\u212a", id="kelvin_sign"),
    ],
)
def test_scheme_keeps_previous_ignorecase_character_coverage(letter: str) -> None:
    """以前のIGNORECASEで認識した文字を含むschemeも秘匿範囲から外さない。"""
    text = f"failed {letter}://user:synthetic@host"
    assert redact_url_userinfo(text) == (
        f"failed {letter}://[redacted:url_userinfo]@host"
    )


def test_multiple_prefixed_urls_are_protected_independently() -> None:
    """先のURLを置換しても後続のURLのscheme境界を巻き込まない。"""
    text = "-https://user:synthetic@host 1postgresql+asyncpg://user:synthetic@db"
    assert redact_url_userinfo(text) == (
        "-https://[redacted:url_userinfo]@host "
        "1postgresql+asyncpg://[redacted:url_userinfo]@db"
    )


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("postgres://db:5432/vector", id="connection_url"),
        pytest.param("https://example.invalid/" + "x" * 32768, id="long_public_url"),
    ],
)
def test_url_without_userinfo_is_preserved(text: str) -> None:
    """認証部分のない接続URLや長い公開URLを置換しない。"""
    assert redact_url_userinfo(text) == text
