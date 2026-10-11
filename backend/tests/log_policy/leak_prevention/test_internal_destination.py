"""文字列に現れた内部の宛先を種別付きの表記へ置き換え、公開の宛先と通常の文を残す境界。"""

import pytest

from app.log_policy.internal_destination import redact_internal_destinations

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "Client error '403 Forbidden' for url 'http://10.0.0.5/article?id=1'",
            "Client error '403 Forbidden' for url '[redacted:internal_url]'",
            id="private_ipv4_in_httpx_status_message",
        ),
        pytest.param(
            "fetch http://[::1]:8080/a failed",
            "fetch [redacted:internal_url] failed",
            id="ipv6_loopback",
        ),
        pytest.param(
            "fetch http://[::ffff:10.0.0.5]/a failed",
            "fetch [redacted:internal_url] failed",
            id="ipv4_mapped_ipv6",
        ),
        pytest.param(
            "fetch http://[fe80::1%25eth0]/a failed",
            "fetch [redacted:internal_url] failed",
            id="link_local_with_zone",
        ),
        pytest.param(
            "fetch http://localhost:3000/a failed",
            "fetch [redacted:internal_url] failed",
            id="localhost",
        ),
        pytest.param(
            "fetch http://api.localhost/a failed",
            "fetch [redacted:internal_url] failed",
            id="localhost_subdomain",
        ),
        pytest.param(
            "fetch http://%31%30.0.0.5/a failed",
            "fetch [redacted:internal_url] failed",
            id="percent_encoded_host",
        ),
        pytest.param(
            "fetch http://１０.0.0.5/a failed",
            "fetch [redacted:internal_url] failed",
            id="fullwidth_digits_host",
        ),
    ],
)
def test_url_to_internal_destination_is_replaced_whole(
    text: str, expected: str
) -> None:
    """内部の宛先を指すURLは、pathやqueryを含めて全体を置き換える。"""
    assert redact_internal_destinations(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "fetch http://2130706433/a failed",
            "fetch [redacted:ambiguous_url] failed",
            id="decimal_integer_host",
        ),
        pytest.param(
            "fetch http://0x7f.0.0.1/a failed",
            "fetch [redacted:ambiguous_url] failed",
            id="hex_octet_host",
        ),
        pytest.param(
            "fetch http://010.0.0.1/a failed",
            "fetch [redacted:ambiguous_url] failed",
            id="leading_zero_octet_host",
        ),
        pytest.param(
            "fetch http://[::1/a failed",
            "fetch [redacted:ambiguous_url] failed",
            id="unbalanced_ipv6_bracket",
        ),
    ],
)
def test_url_with_ambiguous_host_is_not_kept_as_is(text: str, expected: str) -> None:
    """解釈によって内部を指しうる宛先や解析できないURLは、原文を残さず置き換える。"""
    assert redact_internal_destinations(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "not a public IP: 10.0.0.5",
            "not a public IP: [redacted:internal_ip]",
            id="destination_policy_message",
        ),
        pytest.param(
            "Connect call failed ('127.0.0.1', 5433)",
            "Connect call failed ('[redacted:internal_ip]', 5433)",
            id="asyncio_connect_ipv4",
        ),
        pytest.param(
            "Connect call failed ('::1', 5433, 0, 0)",
            "Connect call failed ('[redacted:internal_ip]', 5433, 0, 0)",
            id="asyncio_connect_ipv6",
        ),
        pytest.param(
            "connection to 10.0.0.5:5432 refused",
            "connection to [redacted:internal_ip]:5432 refused",
            id="ipv4_with_port",
        ),
        pytest.param(
            "host resolves to non-public address: ::ffff:192.168.0.1",
            "host resolves to non-public address: [redacted:internal_ip]",
            id="ipv4_mapped_ipv6",
        ),
        pytest.param(
            "route via fe80::1%eth0 failed",
            "route via [redacted:internal_ip] failed",
            id="link_local_with_zone",
        ),
        pytest.param(
            "resolved to 169.254.169.254.",
            "resolved to [redacted:internal_ip].",
            id="sentence_end",
        ),
        pytest.param(
            "could not connect to server at localhost:5432",
            "could not connect to server at [redacted:internal_host]:5432",
            id="localhost_host_name",
        ),
    ],
)
def test_internal_address_in_text_is_replaced(text: str, expected: str) -> None:
    """URLでない文に現れた非公開IPとlocalhostだけを置き換え、周囲の文を残す。"""
    assert redact_internal_destinations(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            "Client error '404 Not Found' for url "
            "'https://example.com/news/article?edition=1'",
            id="public_article_url",
        ),
        pytest.param("fetch http://8.8.8.8/a failed", id="public_ipv4_url"),
        pytest.param("resolved to 8.8.8.8", id="public_ipv4"),
        pytest.param("resolved to 2606:4700:4700::1111", id="public_ipv6"),
        pytest.param("version 1.2.3.4.5 released", id="version_string"),
        pytest.param("started at 12:30:45.123", id="time_of_day"),
    ],
)
def test_public_destination_and_plain_text_are_kept(text: str) -> None:
    """公開の宛先と、IPに似た通常の文は変えない。"""
    assert redact_internal_destinations(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "fetch http://[redacted:url_userinfo]@10.0.0.5/a failed",
            "fetch [redacted:internal_url] failed",
            id="internal_host",
        ),
        pytest.param(
            "connection to postgresql://[redacted:url_userinfo]@db:5432/v refused",
            "connection to postgresql://[redacted:url_userinfo]@db:5432/v refused",
            id="non_internal_host",
        ),
    ],
)
def test_host_is_judged_after_userinfo_replacement(text: str, expected: str) -> None:
    """認証情報の置換表記が残るuserinfoを除いてhostを判定する。"""
    assert redact_internal_destinations(text) == expected
