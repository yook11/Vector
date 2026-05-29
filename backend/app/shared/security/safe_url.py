"""検証済み HTTP/HTTPS URL の値オブジェクト。

SafeUrl は URL が安全なスキーム (http または https) を使い、
正しい構造を持つことを保証する。検証は Pydantic の AnyHttpUrl に
委譲するが、格納される値は元の文字列 (strip 後) で、
正規化は行わない。

加えて、ホストが IP リテラルである場合は ``PublicIpAddress`` 経由で
public IP に該当することを保証し、private/loopback 等を構造的に拒否
する (SSRF defense-in-depth)。DNS 名のリゾルブはここでは行わない:
それは実フェッチ層 (``ssrf_guard.ensure_host_is_public``) の責務。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar
from urllib.parse import urlparse

from pydantic import (
    AnyHttpUrl,
    ConfigDict,
    RootModel,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from app.shared.security.ssrf_guard import (
    NotAnIpAddressError,
    NotAPublicIpError,
    PublicIpAddress,
)

_url_adapter = TypeAdapter(AnyHttpUrl)
_MAX_LENGTH = 2048


class SafeUrlInvalidReason(StrEnum):
    """SafeUrl 検証の失敗理由。値だけで原因が読めるよう監査に焼く粒度にする。"""

    URL_NOT_A_STRING = "url_not_a_string"
    URL_EMPTY = "url_empty"
    URL_TOO_LONG = "url_too_long"
    URL_NOT_HTTP = "url_not_http"
    HOST_NOT_PUBLIC_IP = "host_not_public_ip"


class SafeUrlInvalidError(ValueError):
    """SafeUrl として検証できない入力。reason で失敗段を構造化する。

    ``ValueError`` サブクラスなので ``SafeUrl`` の validator 内で raise すると
    pydantic が ``ValidationError`` にラップする (既存 ``SafeUrl(x)`` 契約維持)。
    ``CanonicalArticleUrl.from_raw`` は validator を直接呼び reason を型で取る。
    URL 値 / IP などの input は載せず reason タグのみを監査へ流す (PII フリー)。
    """

    MESSAGE: ClassVar[str] = "value is not a valid safe URL"

    def __init__(self, *, reason: SafeUrlInvalidReason) -> None:
        self.reason = reason
        super().__init__(f"{self.MESSAGE}: {reason}")


class SafeUrl(RootModel[str]):
    """Pydantic によって検証された HTTP/HTTPS URL。

    Invariants:
    - http または https スキームを使用
    - 有効な URL 構造 (最低でも scheme + host)
    - ホストが IP リテラルなら ``PublicIpAddress`` として valid
      (例: ``http://169.254.169.254/`` は拒否)
    - トリム後 1-2048 文字
    - 生成後は不変
    """

    model_config = ConfigDict(frozen=True)

    @field_validator("root", mode="before")
    @classmethod
    def _validate(cls, v: Any) -> str:
        """SafeUrl の不変条件を検証し strip 済み値を返す。

        何が起きたらどの reason を出すかを、この振る舞いの中で示す。raise する
        ``SafeUrlInvalidError`` は ``ValueError`` サブクラスなので pydantic が
        ``ValidationError`` にラップする (``SafeUrl(x)`` の契約維持)。
        ``CanonicalArticleUrl.from_raw`` は本 validator を直接呼び reason を型で
        受け取る (pydantic 非経由で ``__cause__`` も保たれる)。
        """
        if not isinstance(v, str):
            raise SafeUrlInvalidError(reason=SafeUrlInvalidReason.URL_NOT_A_STRING)
        v = v.strip()
        if not v:
            raise SafeUrlInvalidError(reason=SafeUrlInvalidReason.URL_EMPTY)
        if len(v) > _MAX_LENGTH:
            raise SafeUrlInvalidError(reason=SafeUrlInvalidReason.URL_TOO_LONG)
        try:
            _url_adapter.validate_python(v)
        except ValidationError as e:
            raise SafeUrlInvalidError(reason=SafeUrlInvalidReason.URL_NOT_HTTP) from e
        host = urlparse(v).hostname
        if host:
            try:
                PublicIpAddress(host)
            except NotAnIpAddressError:
                # DNS 名は SafeUrl 単独では判定できない (実フェッチ層で検証)
                pass
            except NotAPublicIpError as e:
                raise SafeUrlInvalidError(
                    reason=SafeUrlInvalidReason.HOST_NOT_PUBLIC_IP
                ) from e
        return v

    def __str__(self) -> str:
        return self.root

    def __repr__(self) -> str:
        return f"SafeUrl({self.root!r})"
