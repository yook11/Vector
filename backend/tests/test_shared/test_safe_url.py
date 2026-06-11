"""SafeUrl 値オブジェクトのテスト。"""

import json

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from app.shared.security.safe_url import (
    SafeUrl,
    SafeUrlInvalidError,
    SafeUrlInvalidReason,
)


class TestSafeUrl:
    def test_valid_https(self) -> None:
        url = SafeUrl("https://example.com/path")
        assert url.root == "https://example.com/path"
        assert str(url) == "https://example.com/path"

    def test_valid_http(self) -> None:
        url = SafeUrl("http://example.com")
        assert url.root == "http://example.com"

    def test_valid_with_query_and_fragment(self) -> None:
        raw = "https://example.com/search?q=test&page=1#results"
        url = SafeUrl(raw)
        assert url.root == raw

    def test_strips_whitespace(self) -> None:
        url = SafeUrl("  https://example.com  ")
        assert url.root == "https://example.com"

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            SafeUrl("")

    def test_rejects_whitespace_only(self) -> None:
        with pytest.raises(ValidationError):
            SafeUrl("   ")

    def test_rejects_javascript_scheme(self) -> None:
        with pytest.raises(ValidationError):
            SafeUrl("javascript:alert(1)")

    def test_rejects_data_scheme(self) -> None:
        with pytest.raises(ValidationError):
            SafeUrl("data:text/html,<h1>hi</h1>")

    def test_rejects_ftp_scheme(self) -> None:
        with pytest.raises(ValidationError):
            SafeUrl("ftp://files.example.com")

    def test_rejects_no_scheme(self) -> None:
        with pytest.raises(ValidationError):
            SafeUrl("example.com")

    def test_rejects_non_string(self) -> None:
        with pytest.raises(ValidationError):
            SafeUrl(123)  # type: ignore[arg-type]

    def test_accepts_exactly_max_length(self) -> None:
        # _MAX_LENGTH (2048) ちょうどは通る境界
        prefix = "https://example.com/"
        url = prefix + "a" * (2048 - len(prefix))
        assert len(url) == 2048
        assert SafeUrl(url).root == url

    def test_rejects_one_over_max_length(self) -> None:
        # _MAX_LENGTH + 1 (2049) は弾かれる境界
        prefix = "https://example.com/"
        url = prefix + "a" * (2049 - len(prefix))
        assert len(url) == 2049
        with pytest.raises(ValidationError):
            SafeUrl(url)

    def test_equality(self) -> None:
        assert SafeUrl("https://a.com") == SafeUrl("https://a.com")
        assert SafeUrl("https://a.com") != SafeUrl("https://b.com")

    def test_equality_different_type(self) -> None:
        url = SafeUrl("https://example.com")
        assert url != "https://example.com"
        assert url != 42

    def test_hash_consistency(self) -> None:
        a = SafeUrl("https://example.com")
        b = SafeUrl("https://example.com")
        assert hash(a) == hash(b)
        assert len({a, b}) == 1

    def test_immutable(self) -> None:
        url = SafeUrl("https://example.com")
        with pytest.raises(ValidationError, match="frozen"):
            url.root = "https://hacked.com"  # type: ignore[misc]

    def test_repr(self) -> None:
        assert repr(SafeUrl("https://example.com")) == "SafeUrl('https://example.com')"


# SafeUrl — SSRF Defense (IP リテラル拒否)
class TestSafeUrlBlocksPrivateIpLiterals:
    """ホストが private/loopback/link-local/... の IP リテラルなら拒否する。

    DNS 名はここでは判定不能なので拒否されない (実フェッチ層の責務)。
    """

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/",
            "http://127.0.0.1:8000/admin",
            "http://10.0.0.1/",
            "http://172.16.0.1/",
            "http://192.168.1.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://0.0.0.0/",
            "http://224.0.0.1/",
            "http://[::1]/",
            "http://[fe80::1]/",
            "http://[fc00::1]/",
        ],
    )
    def test_rejects_private_ip_literal(self, url: str) -> None:
        with pytest.raises(ValidationError):
            SafeUrl(url)

    @pytest.mark.parametrize(
        "url",
        [
            "http://8.8.8.8/",
            "https://1.1.1.1/dns-query",
            "http://[2001:4860:4860::8888]/",
        ],
    )
    def test_accepts_public_ip_literal(self, url: str) -> None:
        SafeUrl(url)

    def test_accepts_dns_name(self) -> None:
        # DNS 名は SafeUrl 単独では判定しない
        SafeUrl("https://example.com/")
        SafeUrl("https://backend/")  # docker compose のサービス名は実フェッチ層で判定


class TestPydanticIntegration:
    class SampleModel(BaseModel):
        url: SafeUrl

    def test_model_from_str(self) -> None:
        m = self.SampleModel(url="https://example.com")
        assert isinstance(m.url, SafeUrl)
        assert m.url.root == "https://example.com"

    def test_model_from_value_object(self) -> None:
        url = SafeUrl("https://example.com")
        m = self.SampleModel(url=url)
        assert isinstance(m.url, SafeUrl)

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
        assert schema["$defs"]["SafeUrl"]["type"] == "string"

    def test_from_attributes(self) -> None:
        class OrmLike:
            def __init__(self, url: str) -> None:
                self.url = url

        class ModelWithFromAttributes(BaseModel):
            model_config = ConfigDict(from_attributes=True)
            url: SafeUrl

        orm_obj = OrmLike(url="https://example.com")
        m = ModelWithFromAttributes.model_validate(orm_obj)
        assert isinstance(m.url, SafeUrl)
        assert m.url.root == "https://example.com"


# SafeUrl — 失敗理由 (reason) の所有テスト
class TestSafeUrlValidateReason:
    """``SafeUrl`` 検証が失敗段を ``SafeUrlInvalidReason`` で分類することの所有テスト。

    reason は ``SafeUrl(x)`` 経由だと pydantic の ValidationError ctx に潜るため、
    検証本体 ``_validate`` を直接呼んで型で確かめる
    (``CanonicalArticleUrl.from_raw`` が消費するのと同じ経路)。
    """

    @pytest.mark.parametrize(
        ("raw", "expected_reason"),
        [
            (123, SafeUrlInvalidReason.URL_NOT_A_STRING),
            ("", SafeUrlInvalidReason.URL_EMPTY),
            ("   ", SafeUrlInvalidReason.URL_EMPTY),
            ("https://example.com/" + "a" * 2040, SafeUrlInvalidReason.URL_TOO_LONG),
            ("ftp://files.example.com", SafeUrlInvalidReason.URL_NOT_HTTP),
            ("javascript:alert(1)", SafeUrlInvalidReason.URL_NOT_HTTP),
            ("example.com", SafeUrlInvalidReason.URL_NOT_HTTP),
            ("http://127.0.0.1/", SafeUrlInvalidReason.HOST_NOT_PUBLIC_IP),
            ("http://169.254.169.254/", SafeUrlInvalidReason.HOST_NOT_PUBLIC_IP),
        ],
    )
    def test_validate_classifies_failure_reason(
        self, raw: object, expected_reason: SafeUrlInvalidReason
    ) -> None:
        with pytest.raises(SafeUrlInvalidError) as exc_info:
            SafeUrl._validate(raw)
        assert exc_info.value.reason is expected_reason
