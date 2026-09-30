"""実Squidの宛先・送信元・管理機能の制限を通信結果で検証する。"""

import pytest


def test_allowed_domain_reaches_origin(proxy_trial) -> None:
    """許可ドメインへの要求はSquidを通り、宛先サーバーの応答を返す。"""
    response = proxy_trial.get("http://allowed.test/article")

    assert response == {"status": 200, "body": "squid-test-origin"}
    assert proxy_trial.origin_observation() == {
        "connections": 1,
        "requests": [{"host": "allowed.test", "path": "/article"}],
    }


def test_unlisted_domain_is_denied_before_origin_connection(proxy_trial) -> None:
    """同じ許可IP・ポートでも、許可外ドメインは403で拒否して接続しない。"""
    response = proxy_trial.get("http://blocked.test/article")

    assert response["status"] == 403
    assert proxy_trial.origin_observation() == {"connections": 0, "requests": []}


def test_article_fetch_can_reach_an_unlisted_public_domain(proxy_trial) -> None:
    """ドメインを限定しない記事取得の送信元は、未登録の公開宛先へ接続できる。"""
    response = proxy_trial.get(
        "http://unlisted.test/article", source_ip="93.184.216.22"
    )

    assert response == {"status": 200, "body": "squid-test-origin"}
    assert proxy_trial.origin_observation() == {
        "connections": 1,
        "requests": [{"host": "unlisted.test", "path": "/article"}],
    }


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://10.254.216.20/article", id="private-ipv4"),
        pytest.param("http://[fd00:285::20]/article", id="private-ipv6"),
    ],
)
def test_article_fetch_cannot_reach_a_private_ip(proxy_trial, url: str) -> None:
    """ドメイン無制限の送信元でも、非公開IPへの外部通信要求は接続前に拒否する。"""
    proxy_trial.check_destination_reachable(url)
    before = proxy_trial.origin_observation()

    response = proxy_trial.get(url, source_ip="93.184.216.22")

    assert response["status"] == 403
    assert proxy_trial.origin_observation() == before


def test_allowed_domain_resolving_to_private_ip_is_denied(proxy_trial) -> None:
    """許可ドメインでも、SquidのDNS解決先が非公開IPなら接続前に拒否する。"""
    proxy_trial.check_destination_reachable("http://private.test/article")
    before = proxy_trial.origin_observation()

    response = proxy_trial.get("http://private.test/article")

    assert response["status"] == 403
    assert proxy_trial.origin_observation() == before


def test_allowed_domain_on_unapproved_port_is_denied(proxy_trial) -> None:
    """許可ドメイン・公開IPでも、許可外の8080番への要求は接続前に拒否する。"""
    proxy_trial.check_destination_reachable("http://allowed.test:8080/article")
    before = proxy_trial.origin_observation(port=8080)

    response = proxy_trial.get("http://allowed.test:8080/article")

    assert response["status"] == 403
    assert proxy_trial.origin_observation(port=8080) == before


def test_source_b_can_reach_its_own_allowed_destination(proxy_trial) -> None:
    """送信元Bは、B専用の許可ルールに従ってsource-b.testへ接続できる。"""
    response = proxy_trial.get(
        "http://source-b.test/article", source_ip="93.184.216.21"
    )

    assert response == {"status": 200, "body": "squid-test-origin"}
    assert proxy_trial.origin_observation() == {
        "connections": 1,
        "requests": [{"host": "source-b.test", "path": "/article"}],
    }


def test_source_a_cannot_use_source_b_destination_permission(proxy_trial) -> None:
    """送信元Aから、送信元Bにだけ許可された接続先への外部通信要求を拒否する。"""
    # A（.20）にはallowed.testとprivate.test、B（.21）にはsource-b.testだけを許可する。
    response = proxy_trial.get(
        "http://source-b.test/article", source_ip="93.184.216.20"
    )

    assert response["status"] == 403
    assert proxy_trial.origin_observation() == {"connections": 0, "requests": []}


def test_unregistered_source_cannot_send_external_requests(proxy_trial) -> None:
    """許可ルールに登録されていない送信元IPからの外部通信要求を拒否する。"""
    # .23はどの送信元ルールにも登録せず、他の送信元なら許可される宛先を要求する。
    response = proxy_trial.get("http://allowed.test/article", source_ip="93.184.216.23")

    assert response["status"] == 403
    assert proxy_trial.origin_observation() == {"connections": 0, "requests": []}


def test_article_fetch_cannot_access_squid_management(proxy_trial) -> None:
    """外部の記事取得を許可した送信元でも、Squid自身の管理機能への要求は拒否する。"""
    response = proxy_trial.get(
        "http://unlisted.test/squid-internal-mgr/menu", source_ip="93.184.216.22"
    )

    assert response["status"] == 403
    assert proxy_trial.origin_observation() == {"connections": 0, "requests": []}


def test_allowed_https_destination_returns_origin_response(proxy_trial) -> None:
    """許可ドメインの443番はCONNECTと証明書検証を通り、HTTPSの応答を返す。"""
    response = proxy_trial.get("https://allowed.test/article")

    assert response == {"status": 200, "body": "squid-test-origin"}
    assert proxy_trial.origin_observation(port=443) == {
        "connections": 1,
        "requests": [{"host": "allowed.test", "path": "/article"}],
    }


def test_connect_to_unlisted_domain_is_denied(proxy_trial) -> None:
    """公開IP・443番でも、許可外ドメインへのCONNECTは接続前に拒否する。"""
    response = proxy_trial.connect("blocked.test:443")

    assert response["status"] == 403
    assert proxy_trial.origin_observation(port=443) == {
        "connections": 0,
        "requests": [],
    }


@pytest.mark.parametrize(
    ("url", "authority"),
    [
        pytest.param(
            "https://10.254.216.20/article", "10.254.216.20:443", id="private-ipv4"
        ),
        pytest.param(
            "https://[fd00:285::20]/article", "[fd00:285::20]:443", id="private-ipv6"
        ),
    ],
)
def test_article_fetch_connect_to_private_ip_is_denied(
    proxy_trial, url: str, authority: str
) -> None:
    """ドメイン無制限でも、非公開IPへのCONNECTは接続前に拒否する。"""
    proxy_trial.check_destination_reachable(url)
    before = proxy_trial.origin_observation(port=443)

    response = proxy_trial.connect(authority, source_ip="93.184.216.22")

    assert response["status"] == 403
    assert proxy_trial.origin_observation(port=443) == before


def test_connect_to_allowed_domain_resolving_to_private_ip_is_denied(
    proxy_trial,
) -> None:
    """許可ドメインの443番でも、DNS解決先が非公開IPならCONNECTを拒否する。"""
    proxy_trial.check_destination_reachable("https://private.test/article")
    before = proxy_trial.origin_observation(port=443)

    response = proxy_trial.connect("private.test:443")

    assert response["status"] == 403
    assert proxy_trial.origin_observation(port=443) == before


def test_connect_to_http_port_is_denied(proxy_trial) -> None:
    """通常のHTTPで許可する80番でも、CONNECTの接続先としては拒否する。"""
    proxy_trial.check_destination_reachable("http://allowed.test/article")
    before = proxy_trial.origin_observation()

    response = proxy_trial.connect("allowed.test:80")

    assert response["status"] == 403
    assert proxy_trial.origin_observation() == before
