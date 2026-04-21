"""記事候補 VO — ingestion 境界の中間表現。

外部配信形式 (RSS / HN API 等) から ingestion 境界を越える際の正規化済みデータを
表現する。生文字列からの構築は ``ArticleCandidate.from_external`` 経由で行い、
URL 安全性とタイトル整形 (HTML 除去・長さ上限) を構造的に保証する。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from app.domain.safe_url import SafeUrl
from app.utils.sanitize import strip_html_tags

_TITLE_MAX_LENGTH = 500


@dataclass(frozen=True)
class ArticleCandidate:
    """フェッチャーが生成する記事の中間表現。"""

    url: SafeUrl
    title: str

    @classmethod
    def from_external(cls, *, raw_url: str, raw_title: str) -> ArticleCandidate | None:
        """外部ソースの生文字列から候補を構築する。

        正規化に失敗する（不正 URL / 空タイトル）場合は ``None`` を返し、
        呼び出し側でエントリをスキップする運用を想定する。
        """
        if not raw_url:
            return None
        try:
            safe_url = SafeUrl(raw_url)
        except (ValueError, ValidationError):
            return None

        clean_title = (strip_html_tags(raw_title) or "")[:_TITLE_MAX_LENGTH]
        if not clean_title:
            return None

        return cls(url=safe_url, title=clean_title)
