"""取得したコンテンツをクリーンアップするテキスト整形ユーティリティ。"""

import html
import re
from urllib.parse import urlparse

_TAG_RE = re.compile(r"<[^>]+>")

# --- XSS対策: URLスキームのホワイトリスト ---
#
# 外部データに含まれるURLのスキームを検証する共通ユーティリティ。
# javascript:, data:, vbscript: 等の危険なスキームを排除し、
# http/https のみを許可する。
#
# 2つの関数を用途別に提供:
#   is_safe_url()         — bool を返す（フェッチャー等の条件分岐向け）
#   validate_url_scheme() — 不正時に ValueError を raise（Pydantic バリデーター向け）
_SAFE_URL_SCHEMES = {"http", "https"}


def is_safe_url(url: str) -> bool:
    """URL が安全なスキーム (http または https) かを判定する。

    フェッチャーが危険な URL スキームを持つ記事を
    DB 保存前に除外するために使う。
    """
    try:
        parsed = urlparse(url)
        return parsed.scheme in _SAFE_URL_SCHEMES and bool(parsed.netloc)
    except Exception:
        return False


def validate_url_scheme(url: str, field_name: str = "url") -> str:
    """URL が http または https スキームであることを検証する。

    Pydantic の field_validator 互換のため不正時は ValueError を送出。
    API 境界で安全でない URL を拒否する Pydantic スキーマで使用。
    """
    if not is_safe_url(url):
        raise ValueError(f"{field_name} must be a valid http or https URL")
    return url


def strip_html_tags(text: str | None) -> str | None:
    """テキストから HTML タグを除去し HTML エンティティをデコードする。

    入力が None の場合は None を返す。
    """
    if text is None:
        return None
    cleaned = _TAG_RE.sub("", text)
    return html.unescape(cleaned).strip()
