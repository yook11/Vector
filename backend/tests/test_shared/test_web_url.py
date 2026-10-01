"""WebUrl 値オブジェクトのテスト。"""

import json

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from app.shared.web_url import (
    WebUrl,
    WebUrlInvalidError,
    WebUrlInvalidReason,
)


class TestWebUrl:
    def test_valid_https(self) -> None:
        url = WebUrl("https://example.com/path")
        assert url.root == "https://example.com/path"
        assert str(url) == "https://example.com/path"

    def test_valid_http(self) -> None:
        url = WebUrl("http://example.com/")
        assert url.root == "http://example.com/"

    def test_valid_with_query_and_fragment(self) -> None:
        raw = "https://example.com/search?q=test&page=1#results"
        url = WebUrl(raw)
        assert url.root == raw

    def test_strips_whitespace(self) -> None:
        url = WebUrl("  https://example.com/  ")
        assert url.root == "https://example.com/"

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            WebUrl("")

    def test_rejects_whitespace_only(self) -> None:
        with pytest.raises(ValidationError):
            WebUrl("   ")

    def test_rejects_javascript_scheme(self) -> None:
        with pytest.raises(ValidationError):
            WebUrl("javascript:alert(1)")

    def test_rejects_data_scheme(self) -> None:
        with pytest.raises(ValidationError):
            WebUrl("data:text/html,<h1>hi</h1>")

    def test_rejects_ftp_scheme(self) -> None:
        with pytest.raises(ValidationError):
            WebUrl("ftp://files.example.com")

    def test_rejects_no_scheme(self) -> None:
        with pytest.raises(ValidationError):
            WebUrl("example.com")

    def test_rejects_non_string(self) -> None:
        with pytest.raises(ValidationError):
            WebUrl(123)  # type: ignore[arg-type]

    def test_accepts_exactly_max_length(self) -> None:
        # _MAX_LENGTH (2048) ちょうどは通る境界
        prefix = "https://example.com/"
        url = prefix + "a" * (2048 - len(prefix))
        assert len(url) == 2048
        assert WebUrl(url).root == url

    def test_rejects_one_over_max_length(self) -> None:
        # _MAX_LENGTH + 1 (2049) は弾かれる境界
        prefix = "https://example.com/"
        url = prefix + "a" * (2049 - len(prefix))
        assert len(url) == 2049
        with pytest.raises(ValidationError):
            WebUrl(url)

    def test_equality(self) -> None:
        assert WebUrl("https://a.com") == WebUrl("https://a.com")
        assert WebUrl("https://a.com") != WebUrl("https://b.com")

    def test_equality_different_type(self) -> None:
        url = WebUrl("https://example.com")
        assert url != "https://example.com"
        assert url != 42

    def test_hash_consistency(self) -> None:
        a = WebUrl("https://example.com")
        b = WebUrl("https://example.com")
        assert hash(a) == hash(b)
        assert len({a, b}) == 1

    def test_immutable(self) -> None:
        url = WebUrl("https://example.com")
        with pytest.raises(ValidationError, match="frozen"):
            url.root = "https://hacked.com"  # type: ignore[misc]

    def test_repr(self) -> None:
        assert repr(WebUrl("https://example.com/")) == "WebUrl('https://example.com/')"


# WebUrl — pydantic による正規化
class TestWebUrlHoldsNormalizedValue:
    """pydantic が解析して正規化した文字列を値として持つ。

    期待値は WHATWG URL Standard の直列化 (pydantic-core が準拠) による。
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://Example.COM/Path", "https://example.com/Path"),
            ("https://example.com", "https://example.com/"),
            ("https://example.com:443/a", "https://example.com/a"),
            ("https://example.com/a/../b", "https://example.com/b"),
            ("https://example.com/a b", "https://example.com/a%20b"),
            (
                "https://example.com/news/China\u2019s",
                "https://example.com/news/China%E2%80%99s",
            ),
            ("https://b\u00fccher.example/", "https://xn--bcher-kva.example/"),
            ("http://0x7f000001/", "http://127.0.0.1/"),
            ("http://127.0.0.1\\@evil.example/", "http://127.0.0.1/@evil.example/"),
        ],
    )
    def test_holds_pydantic_normalized_value(self, raw: str, expected: str) -> None:
        assert WebUrl(raw).root == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "https://Example.COM",
            "https://example.com/a b?q=1#frag",
            "http://127.0.0.1\\@evil.example/",
        ],
    )
    def test_normalized_value_is_stable(self, raw: str) -> None:
        once = WebUrl(raw)
        assert WebUrl(once.root) == once

    @pytest.mark.parametrize(
        ("raw", "scheme", "host", "port"),
        [
            ("http://0x7f000001/", "http", "127.0.0.1", None),
            ("http://127.1/", "http", "127.0.0.1", None),
            ("http://0177.0.0.1/", "http", "127.0.0.1", None),
            ("http://127.0.0.1\\@evil.example/", "http", "127.0.0.1", None),
            ("http://evil.example\\@127.0.0.1/", "http", "evil.example", None),
            ("https://b\u00fccher.example/", "https", "xn--bcher-kva.example", None),
            ("https://Example.com:8443/x", "https", "example.com", 8443),
        ],
    )
    def test_sender_interprets_same_destination(
        self, raw: str, scheme: str, host: str, port: int | None
    ) -> None:
        """送信に使う httpx も、正規化した値を同じ宛先として解釈する。"""
        sent = httpx.URL(WebUrl(raw).root)
        assert (sent.scheme, sent.raw_host.decode("ascii"), sent.port) == (
            scheme,
            host,
            port,
        )


# WebUrl — 宛先の判定を持たない
class TestWebUrlLeavesDestinationToSendBoundary:
    """ホストの宛先方針は判定せず、形式が正しければ受け付ける。

    非公開IPや内部名の拒否は ``app.http`` の送信境界が送信時に行う。
    """

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8000/admin",
            "http://10.0.0.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/",
            "http://8.8.8.8/",
            "https://backend/",
        ],
    )
    def test_accepts_any_host_with_valid_format(self, url: str) -> None:
        assert WebUrl(url).root == url


class TestPydanticIntegration:
    class SampleModel(BaseModel):
        url: WebUrl

    def test_model_from_str(self) -> None:
        m = self.SampleModel(url="https://example.com/")
        assert isinstance(m.url, WebUrl)
        assert m.url.root == "https://example.com/"

    def test_model_from_value_object(self) -> None:
        url = WebUrl("https://example.com/")
        m = self.SampleModel(url=url)
        assert isinstance(m.url, WebUrl)

    def test_model_dump_unwraps_to_str(self) -> None:
        m = self.SampleModel(url="https://example.com/")
        data = m.model_dump()
        assert data == {"url": "https://example.com/"}
        assert isinstance(data["url"], str)

    def test_model_dump_json(self) -> None:
        m = self.SampleModel(url="https://example.com/")
        data = json.loads(m.model_dump_json())
        assert data["url"] == "https://example.com/"

    def test_model_rejects_invalid(self) -> None:
        with pytest.raises(ValidationError):
            self.SampleModel(url="javascript:alert(1)")

    def test_json_schema_is_string_type(self) -> None:
        schema = self.SampleModel.model_json_schema()
        assert schema["$defs"]["WebUrl"]["type"] == "string"

    def test_from_attributes(self) -> None:
        class OrmLike:
            def __init__(self, url: str) -> None:
                self.url = url

        class ModelWithFromAttributes(BaseModel):
            model_config = ConfigDict(from_attributes=True)
            url: WebUrl

        orm_obj = OrmLike(url="https://example.com/")
        m = ModelWithFromAttributes.model_validate(orm_obj)
        assert isinstance(m.url, WebUrl)
        assert m.url.root == "https://example.com/"


# WebUrl — 失敗理由 (reason) の所有テスト
class TestWebUrlValidateReason:
    """``WebUrl`` 検証が失敗段を ``WebUrlInvalidReason`` で分類することの所有テスト。

    reason は ``WebUrl(x)`` 経由だと pydantic の ValidationError ctx に潜るため、
    公開の入口 ``from_raw`` で型として確かめる。
    """

    @pytest.mark.parametrize(
        ("raw", "expected_reason"),
        [
            (123, WebUrlInvalidReason.URL_NOT_A_STRING),
            ("", WebUrlInvalidReason.URL_EMPTY),
            ("   ", WebUrlInvalidReason.URL_EMPTY),
            ("https://example.com/" + "a" * 2040, WebUrlInvalidReason.URL_TOO_LONG),
            # 入力は 270 字だが U+2019 が %E2%80%99 (9字) に展開され 2048 字を超える。
            pytest.param(
                "https://example.com/" + "\u2019" * 250,
                WebUrlInvalidReason.URL_TOO_LONG,
                id="normalized-over-max-length",
            ),
            ("ftp://files.example.com", WebUrlInvalidReason.URL_NOT_HTTP),
            ("javascript:alert(1)", WebUrlInvalidReason.URL_NOT_HTTP),
            ("example.com", WebUrlInvalidReason.URL_NOT_HTTP),
        ],
    )
    def test_from_raw_classifies_failure_reason(
        self, raw: object, expected_reason: WebUrlInvalidReason
    ) -> None:
        with pytest.raises(WebUrlInvalidError) as exc_info:
            WebUrl.from_raw(raw)
        assert exc_info.value.reason is expected_reason

    def test_from_raw_returns_normalized_web_url(self) -> None:
        url = WebUrl.from_raw("  HTTPS://Example.COM  ")
        assert isinstance(url, WebUrl)
        assert url.root == "https://example.com/"


# WebUrl — 解析済みの部品と組み立て直し
class TestWebUrlParts:
    """pydantic が解析した部品を渡し、部品を差し替えた WebUrl を作る。"""

    def test_path_and_query(self) -> None:
        url = WebUrl("https://example.com/a/b?x=1&y=2#top")
        assert (url.path, url.query) == ("/a/b", "x=1&y=2")

    def test_query_is_none_without_query(self) -> None:
        assert WebUrl("https://example.com/a").query is None

    def test_replace_keeps_origin_and_userinfo(self) -> None:
        url = WebUrl("https://user:pw@example.com:8443/a/b?x=1#top")
        replaced = url.replace(path="/c", query="y=2", fragment=None)
        assert replaced == WebUrl("https://user:pw@example.com:8443/c?y=2")

    def test_replace_sets_fragment(self) -> None:
        url = WebUrl("https://example.com/a#top")
        replaced = url.replace(path="/a", query=None, fragment="end")
        assert replaced.root == "https://example.com/a#end"

    def test_replace_rejects_value_over_max_length(self) -> None:
        # 元の URL は 20 字で、差し替えた path で 2048 字を超える
        url = WebUrl("https://example.com/")
        with pytest.raises(WebUrlInvalidError) as exc_info:
            url.replace(path="/" + "a" * 2040, query=None, fragment=None)
        assert exc_info.value.reason is WebUrlInvalidReason.URL_TOO_LONG
