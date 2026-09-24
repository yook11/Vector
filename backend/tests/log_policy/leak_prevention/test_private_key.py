"""PEM秘密鍵をブロック全体で種別付きの表記へ置き換える境界。"""

import pytest

from app.log_policy.leak_prevention import redact_private_key_blocks

pytestmark = pytest.mark.unit

# 合成値を分割し、秘密検出ツールの規則に一致させない。
_BEGIN = "-----BEGIN "
_END = "-----END "


@pytest.mark.parametrize("label", ["", "RSA ", "EC ", "OPENSSH ", "ENCRYPTED "])
def test_private_key_block_is_replaced_as_a_whole(label: str) -> None:
    """種類の有無によらず、BEGIN から END までを本文ごと置き換え、前後の文を残す。"""
    block = (
        f"{_BEGIN}{label}PRIVATE KEY-----\nsynthetic-private-body\n"
        f"{_END}{label}PRIVATE KEY-----"
    )
    assert redact_private_key_blocks(f"failed {block} retry") == (
        "failed [redacted:private_key] retry"
    )


def test_unterminated_private_key_block_is_replaced_to_the_end() -> None:
    """END 行が欠けたブロックは末尾まで置き換え、本文の断片を残さない。"""
    text = f"failed {_BEGIN}PRIVATE KEY-----\nsynthetic-private-body\nmore"
    assert redact_private_key_blocks(text) == "failed [redacted:private_key]"


def test_multiple_private_key_blocks_are_replaced_independently() -> None:
    """複数のブロックをそれぞれ置き換え、間の文を残す。"""
    block = f"{_BEGIN}PRIVATE KEY-----\nsynthetic\n{_END}PRIVATE KEY-----"
    assert redact_private_key_blocks(f"{block} between {block}") == (
        "[redacted:private_key] between [redacted:private_key]"
    )


@pytest.mark.parametrize("label", ["PUBLIC KEY", "CERTIFICATE"])
def test_public_pem_block_is_preserved(label: str) -> None:
    """公開鍵や証明書のブロックは秘密鍵として扱わない。"""
    text = f"{_BEGIN}{label}-----\nsynthetic-public-body\n{_END}{label}-----"
    assert redact_private_key_blocks(text) == text
