"""HTTP専用設定の検証とクライアント生成時の読み込みを確認する。"""

from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.http import external
from app.http.settings import HttpSettings


@pytest.mark.parametrize(
    "value",
    ["http://proxy.vector.internal:3128", "https://proxy.flycast:443"],
)
def test_http_settings_accept_existing_routes(value):
    assert HttpSettings(egress_proxy_url=value).egress_proxy_url == value
    assert HttpSettings.model_config["env_file"] is None
    assert set(HttpSettings.model_fields) == {"egress_proxy_url"}


@pytest.mark.parametrize(
    "value",
    [
        "",
        "http://",
        "http://localhost:3128",
        "http://127.0.0.1:3128",
        "http://evilvector.internal",
        "http://proxy.vector.internal.evil.com",
        "http://example.com",
        "socks5://proxy.vector.internal",
        "file:///tmp/proxy",
    ],
)
def test_http_settings_reject_invalid_routes(value):
    with pytest.raises(ValidationError, match="EGRESS_PROXY_URL"):
        HttpSettings(egress_proxy_url=value)


@pytest.mark.asyncio
async def test_factory_reads_environment_each_time_without_cache(monkeypatch):
    """生成ごとに検証し、不正設定時はtransportを作らない。"""
    constructor = Mock(wraps=external._PinnedDnsTransport)
    monkeypatch.setattr(external, "_PinnedDnsTransport", constructor)
    for value in ("http://proxy.vector.internal:3128", "https://proxy.flycast"):
        monkeypatch.setenv("EGRESS_PROXY_URL", value)
        async with external.make_external_async_client():
            assert constructor.call_args.kwargs["proxy"] == value
    assert constructor.call_count == 2
    monkeypatch.setenv("EGRESS_PROXY_URL", "http://untrusted.invalid")
    with pytest.raises(ValidationError):
        external.make_external_async_client()
    assert constructor.call_count == 2


@pytest.mark.parametrize("value", [None, "", " "])
def test_missing_proxy_never_constructs_transport(monkeypatch, value):
    """環境変数と呼び出し側引数のどちらでも直接接続へ逃がさない。"""
    if value is None:
        monkeypatch.delenv("EGRESS_PROXY_URL", raising=False)
    else:
        monkeypatch.setenv("EGRESS_PROXY_URL", value)
    constructor = Mock()
    monkeypatch.setattr(external, "_PinnedDnsTransport", constructor)
    for kwargs in ({}, {"proxy": None}, {"proxy": "http://proxy.vector.internal"}):
        with pytest.raises(ValidationError):
            external.make_external_async_client(**kwargs)
    constructor.assert_not_called()


def test_explicit_none_is_invalid():
    with pytest.raises(ValidationError):
        HttpSettings(egress_proxy_url=None)
