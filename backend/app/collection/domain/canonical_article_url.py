"""記事同一性用に正規化された URL の値オブジェクト。

CanonicalArticleUrl は ``analyzable_articles.source_url`` /
``incomplete_articles.url`` の SSoT を「永続化キーとして使える形」で
構造保証する VO。canonicalize_url で表記揺れを吸収し、加えて SafeUrl の
不変条件 (http/https + 構文 + SSRF) を canonical 値で満たすことを保証する。

SafeUrl との責務分離:

- ``SafeUrl``: 「危険でない URL」(http/https + 構文 + SSRF defense-in-depth)
  の汎用土台。canonicalize は意図的に行わない (`safe_url.py` 冒頭参照)。
- ``CanonicalArticleUrl``: 「記事 identity として永続化可能な URL」
  (SafeUrl の制約 ∩ canonicalize 済み) の記事ドメイン専用 VO。

継承ではなく合成で表現する。Pydantic v2 の RootModel 継承は validator
発火順序が公式未文書化で MRO 変更で破綻実績があるため、composition で
SafeUrl の検証を呼んで結果を再利用する。

外部 SSRF 境界 (``scraper.scrape`` 等) は SafeUrl を要求するため、
``as_safe_url()`` 経由で受け渡す。canonical かどうかは取得側の関心事
ではなく、責務分離を保つ。
"""

from __future__ import annotations

from typing import Any, ClassVar, Self

from pydantic import ConfigDict, RootModel, field_validator

from app.collection.url_canonicalize import canonicalize_url
from app.shared.security.safe_url import (
    SafeUrl,
    SafeUrlInvalidError,
    SafeUrlInvalidReason,
)


class CanonicalArticleUrlInvalidError(Exception):
    """canonicalize 後の値が SafeUrl 不変条件を満たさず記事 URL にできなかった失敗。

    canonicalize_url は失敗しない (冪等 transform) ため、本失敗は全て SafeUrl 由来。
    reason は下位 SafeUrl の失敗段をそのまま運ぶ (URL/IP の input は載せない)。
    """

    MESSAGE: ClassVar[str] = "value is not a valid canonical article URL"

    def __init__(self, *, reason: SafeUrlInvalidReason) -> None:
        self.reason = reason
        super().__init__(f"{self.MESSAGE}: {reason}")


class CanonicalArticleUrl(RootModel[str]):
    """canonicalize 済みかつ SafeUrl 互換である URL。

    Invariants:
    - canonicalize_url 適用済 (lowercase host / tracking param strip /
      trailing slash / fragment 除去 / scheme 保存)
    - SafeUrl の制約も canonical 値で満たす (http/https + 構文 + SSRF)
    - 生成後は不変

    本 VO のインスタンスが存在する = 「``analyzable_articles.source_url`` /
    ``incomplete_articles.url`` UNIQUE 制約のキーとしてそのまま使える」
    という型レベル保証。
    """

    model_config = ConfigDict(frozen=True)

    @field_validator("root", mode="before")
    @classmethod
    def _normalize(cls, v: Any) -> str:
        if isinstance(v, CanonicalArticleUrl):
            # 冪等: 既に canonical な値なので再正規化不要
            return v.root
        if isinstance(v, SafeUrl):
            raw = v.root
        elif isinstance(v, str):
            raw = v
        else:
            msg = (
                f"Expected str / SafeUrl / CanonicalArticleUrl, got {type(v).__name__}"
            )
            raise ValueError(msg)
        canonical = canonicalize_url(raw)
        # SafeUrl の invariant (構文 + SSRF) を canonical 値で再検証し strip 済み値を
        # 返す。SafeUrlInvalidError は ValueError サブクラスなので pydantic が
        # ValidationError 化する (CanonicalArticleUrl(x) 直接構築の契約を維持)。
        return SafeUrl._validate(canonical)

    @classmethod
    def from_raw(cls, raw: str) -> Self:
        """生 URL を canonicalize → SafeUrl 検証し、失敗時は reason 付き例外へ翻訳する。

        ``CanonicalArticleUrl(x)`` 直接構築は validator 経由で ValidationError を
        維持する (stage1 converter / ORM / テスト用)。本 factory は失敗理由を型で
        運ぶ stage2 用の経路で、``SafeUrl._validate`` を pydantic 非経由で直接呼ぶ
        ため reason を型で受け取れ ``__cause__`` 連鎖も保たれる。
        """
        canonical = canonicalize_url(raw)
        try:
            validated = SafeUrl._validate(canonical)
        except SafeUrlInvalidError as exc:
            raise CanonicalArticleUrlInvalidError(reason=exc.reason) from exc
        return cls(validated)

    def as_safe_url(self) -> SafeUrl:
        """SafeUrl 互換の値を取り出す (SSRF 境界呼出用)。

        validator で SafeUrl 検証を通過した値なので構築は冪等。
        ``scraper.scrape`` などの SafeUrl 要求 API への橋渡しに使う。
        """
        return SafeUrl(self.root)

    def __str__(self) -> str:
        return self.root

    def __repr__(self) -> str:
        return f"CanonicalArticleUrl({self.root!r})"
