"""URL 正規化 — ``articles.source_url`` / ``incomplete_articles.url`` 共通。

URL 一意性のための canonicalize。挙動:

1. host を lowercase 化 (大文字小文字差分による偽 dup を避ける)
2. tracking parameters を除去 (utm_* / fbclid / gclid / dclid / msclkid /
   mc_cid / mc_eid / ref / ref_src / referrer)
3. path 末尾の ``/`` を除去 (root path ``/`` は保持)
4. fragment (``#...``) を除去
5. scheme は保存 (http と https は別 URL として扱う)
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.collection.source_fetch.tools.url_normalizer import _TRACKING_PARAMS


def canonicalize_url(raw: str) -> str:
    """canonicalize 済み URL を返す。

    ``articles.source_url`` / ``incomplete_articles.url`` 共通の正規化。
    冪等: ``canonicalize_url(canonicalize_url(x)) == canonicalize_url(x)``。
    入力が空文字や scheme 欠落でも例外は投げず、urlparse の挙動に従う
    (caller 側で SafeUrl 等の validator を経由している前提)。
    """
    parsed = urlparse(raw)

    netloc = parsed.netloc.lower()

    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    filtered = [(k, v) for k, v in pairs if k.lower() not in _TRACKING_PARAMS]
    new_query = urlencode(filtered, doseq=True)

    path = parsed.path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/") or "/"

    return urlunparse((parsed.scheme, netloc, path, parsed.params, new_query, ""))
