"""記事同一性用に正規化された URL の値オブジェクト。

CanonicalArticleUrl は ``articles.source_url`` /
``pending_html_articles.url`` の SSoT を「永続化キーとして使える形」で
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

外部 SSRF 境界 (``acquirer.acquire`` 等) は SafeUrl を要求するため、
``as_safe_url()`` 経由で受け渡す。canonical かどうかは取得側の関心事
ではなく、責務分離を保つ。
"""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict, RootModel, field_validator

from app.collection.url_canonicalize import canonicalize_url
from app.shared.value_objects.safe_url import SafeUrl


class CanonicalArticleUrl(RootModel[str]):
    """canonicalize 済みかつ SafeUrl 互換である URL。

    Invariants:
    - canonicalize_url 適用済 (lowercase host / tracking param strip /
      trailing slash / fragment 除去 / scheme 保存)
    - SafeUrl の制約も canonical 値で満たす (http/https + 構文 + SSRF)
    - 生成後は不変

    本 VO のインスタンスが存在する = 「``articles.source_url`` /
    ``pending_html_articles.url`` UNIQUE 制約のキーとしてそのまま使える」
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
        # SafeUrl の invariant (構文 + SSRF) を canonical 値で再検証
        SafeUrl(canonical)
        return canonical

    def as_safe_url(self) -> SafeUrl:
        """SafeUrl 互換の値を取り出す (SSRF 境界呼出用)。

        validator で SafeUrl 検証を通過した値なので構築は冪等。
        ``acquirer.acquire`` などの SafeUrl 要求 API への橋渡しに使う。
        """
        return SafeUrl(self.root)

    def __str__(self) -> str:
        return self.root

    def __repr__(self) -> str:
        return f"CanonicalArticleUrl({self.root!r})"
