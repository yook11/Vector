"""検証済み HTTP/HTTPS URL の値オブジェクト。

URL の形式だけを保証し、構文の解析と正規化は Pydantic の AnyHttpUrl に委譲する。
格納される値は正規化後の文字列で、送信時の httpx も同じ宛先として解釈する。
宛先IPの判定は持たず、``app.http`` の送信境界が送信時に行う。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar, Self

from pydantic import (
    AnyHttpUrl,
    ConfigDict,
    RootModel,
    TypeAdapter,
    ValidationError,
    field_validator,
)

_url_adapter = TypeAdapter(AnyHttpUrl)
_MAX_LENGTH = 2048


class WebUrlInvalidReason(StrEnum):
    """WebUrl 検証の失敗理由。値だけで原因が読めるよう監査に焼く粒度にする。"""

    URL_NOT_A_STRING = "url_not_a_string"
    URL_EMPTY = "url_empty"
    URL_TOO_LONG = "url_too_long"
    URL_NOT_HTTP = "url_not_http"


class WebUrlInvalidError(ValueError):
    """WebUrl として検証できない入力。reason で失敗段を構造化する。

    ``ValueError`` サブクラスなので ``WebUrl`` の validator 内で raise すると
    pydantic が ``ValidationError`` にラップする (既存 ``WebUrl(x)`` 契約維持)。
    ``WebUrl.from_raw`` はラップせずにそのまま送出する。
    URL 値などの input は載せず reason タグのみを監査へ流す (PII フリー)。
    """

    MESSAGE: ClassVar[str] = "value is not a valid web URL"

    def __init__(self, *, reason: WebUrlInvalidReason) -> None:
        self.reason = reason
        super().__init__(f"{self.MESSAGE}: {reason}")


class WebUrl(RootModel[str]):
    """Pydantic によって検証された HTTP/HTTPS URL。

    Invariants:
    - http または https スキームを使用
    - 有効な URL 構造 (最低でも scheme + host)
    - 値は Pydantic が正規化した文字列で、再検証しても変わらない
    - 入力と正規化後の値がともに 1-2048 文字
    - 生成後は不変
    """

    model_config = ConfigDict(frozen=True)

    @field_validator("root", mode="before")
    @classmethod
    def _validate(cls, v: Any) -> str:
        """WebUrl の不変条件を検証し正規化した値を返す。

        何が起きたらどの reason を出すかを、この振る舞いの中で示す。raise する
        ``WebUrlInvalidError`` は ``ValueError`` サブクラスなので pydantic が
        ``ValidationError`` にラップする (``WebUrl(x)`` の契約維持)。
        """
        if not isinstance(v, str):
            raise WebUrlInvalidError(reason=WebUrlInvalidReason.URL_NOT_A_STRING)
        v = v.strip()
        if not v:
            raise WebUrlInvalidError(reason=WebUrlInvalidReason.URL_EMPTY)
        if len(v) > _MAX_LENGTH:
            raise WebUrlInvalidError(reason=WebUrlInvalidReason.URL_TOO_LONG)
        try:
            parsed = _url_adapter.validate_python(v)
        except ValidationError as e:
            raise WebUrlInvalidError(reason=WebUrlInvalidReason.URL_NOT_HTTP) from e
        normalized = str(parsed)
        if len(normalized) > _MAX_LENGTH:
            raise WebUrlInvalidError(reason=WebUrlInvalidReason.URL_TOO_LONG)
        return normalized

    @classmethod
    def from_raw(cls, raw: object) -> Self:
        """失敗理由を ValidationError に包まず WebUrlInvalidError で送出する。"""
        return cls.model_construct(cls._validate(raw))

    @property
    def path(self) -> str | None:
        return _url_adapter.validate_python(self.root).path

    @property
    def query(self) -> str | None:
        return _url_adapter.validate_python(self.root).query

    def replace(self, *, path: str, query: str | None, fragment: str | None) -> WebUrl:
        """組み立て直した値も検証し、失敗時は WebUrlInvalidError を送出する。"""
        parsed = _url_adapter.validate_python(self.root)
        rebuilt = AnyHttpUrl.build(
            scheme=parsed.scheme,
            username=parsed.username,
            password=parsed.password,
            host=parsed.host or "",
            port=parsed.port,
            # build は path の先頭に / を補うため、重複しないよう外して渡す。
            path=path.removeprefix("/"),
            query=query,
            fragment=fragment,
        )
        return WebUrl.from_raw(str(rebuilt))

    def __str__(self) -> str:
        return self.root

    def __repr__(self) -> str:
        return f"WebUrl({self.root!r})"
