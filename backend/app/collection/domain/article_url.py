"""記事の同一性に使う URL。記事の2表の UNIQUE キーにそのまま使える形を保証する。

WebUrl の正規化に加えて追跡用パラメータ・path 末尾の ``/``・fragment を除く。
http と https は別の URL として扱う。
"""

from __future__ import annotations

from typing import Any, ClassVar, Self
from urllib.parse import parse_qsl, urlencode

from pydantic import ConfigDict, RootModel, field_validator

from app.shared.web_url import (
    WebUrl,
    WebUrlInvalidError,
    WebUrlInvalidReason,
)

_TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        # UTM (Google Analytics)
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        # 広告クリック ID
        "gclid",  # Google Ads
        "fbclid",  # Facebook
        "dclid",  # DoubleClick
        "msclkid",  # Microsoft Ads
        # Mailchimp
        "mc_cid",
        "mc_eid",
        # 一般的な参照元パラメータ
        "ref",
        "ref_src",
        "referrer",
    }
)


class ArticleUrlInvalidError(Exception):
    """記事 URL にできなかった失敗。監査に URL の値を載せないため reason だけを持つ。"""

    MESSAGE: ClassVar[str] = "value is not a valid article URL"

    def __init__(self, *, reason: WebUrlInvalidReason) -> None:
        self.reason = reason
        super().__init__(f"{self.MESSAGE}: {reason}")


class ArticleUrl(RootModel[str]):
    model_config = ConfigDict(frozen=True)

    @field_validator("root", mode="before")
    @classmethod
    def _validate(cls, v: Any) -> str:
        """入力の長さは追跡用パラメータを除く前に確かめる。"""
        web_url = WebUrl.from_raw(v)

        path = web_url.path or "/"
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/") or "/"

        pairs = parse_qsl(web_url.query or "", keep_blank_values=True)
        kept = [
            (key, value) for key, value in pairs if key.lower() not in _TRACKING_PARAMS
        ]

        return web_url.replace(
            path=path, query=urlencode(kept, doseq=True) or None, fragment=None
        ).root

    @classmethod
    def from_raw(cls, raw: str) -> Self:
        """失敗理由を ValidationError に包まず ArticleUrlInvalidError で送出する。"""
        try:
            value = cls._validate(raw)
        except WebUrlInvalidError as exc:
            raise ArticleUrlInvalidError(reason=exc.reason) from exc
        return cls.model_construct(value)

    def as_web_url(self) -> WebUrl:
        return WebUrl(self.root)

    def __str__(self) -> str:
        return self.root

    def __repr__(self) -> str:
        return f"ArticleUrl({self.root!r})"
