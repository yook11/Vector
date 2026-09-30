"""WebUrl 値オブジェクトのテスト。"""

import json

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
        url = WebUrl("http://example.com")
        assert url.root == "http://example.com"

    def test_valid_with_query_and_fragment(self) -> None:
        raw = "https://example.com/search?q=test&page=1#results"
        url = WebUrl(raw)
        assert url.root == raw

    def test_strips_whitespace(self) -> None:
        url = WebUrl("  https://example.com  ")
        assert url.root == "https://example.com"

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
        assert repr(WebUrl("https://example.com")) == "WebUrl('https://example.com')"


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
        m = self.SampleModel(url="https://example.com")
        assert isinstance(m.url, WebUrl)
        assert m.url.root == "https://example.com"

    def test_model_from_value_object(self) -> None:
        url = WebUrl("https://example.com")
        m = self.SampleModel(url=url)
        assert isinstance(m.url, WebUrl)

    def test_model_dump_unwraps_to_str(self) -> None:
        m = self.SampleModel(url="https://example.com")
        data = m.model_dump()
        assert data == {"url": "https://example.com"}
        assert isinstance(data["url"], str)

    def test_model_dump_json(self) -> None:
        m = self.SampleModel(url="https://example.com")
        data = json.loads(m.model_dump_json())
        assert data["url"] == "https://example.com"

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

        orm_obj = OrmLike(url="https://example.com")
        m = ModelWithFromAttributes.model_validate(orm_obj)
        assert isinstance(m.url, WebUrl)
        assert m.url.root == "https://example.com"


# WebUrl — 失敗理由 (reason) の所有テスト
class TestWebUrlValidateReason:
    """``WebUrl`` 検証が失敗段を ``WebUrlInvalidReason`` で分類することの所有テスト。

    reason は ``WebUrl(x)`` 経由だと pydantic の ValidationError ctx に潜るため、
    検証本体 ``_validate`` を直接呼んで型で確かめる
    (``CanonicalArticleUrl.from_raw`` が消費するのと同じ経路)。
    """

    @pytest.mark.parametrize(
        ("raw", "expected_reason"),
        [
            (123, WebUrlInvalidReason.URL_NOT_A_STRING),
            ("", WebUrlInvalidReason.URL_EMPTY),
            ("   ", WebUrlInvalidReason.URL_EMPTY),
            ("https://example.com/" + "a" * 2040, WebUrlInvalidReason.URL_TOO_LONG),
            ("ftp://files.example.com", WebUrlInvalidReason.URL_NOT_HTTP),
            ("javascript:alert(1)", WebUrlInvalidReason.URL_NOT_HTTP),
            ("example.com", WebUrlInvalidReason.URL_NOT_HTTP),
        ],
    )
    def test_validate_classifies_failure_reason(
        self, raw: object, expected_reason: WebUrlInvalidReason
    ) -> None:
        with pytest.raises(WebUrlInvalidError) as exc_info:
            WebUrl._validate(raw)
        assert exc_info.value.reason is expected_reason
