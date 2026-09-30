"""URL 正規化 — ``analyzable_articles.source_url`` / ``incomplete_articles.url`` 共通。

URL 一意性のための canonicalize。pydantic が解析した部品から組み立て直す。挙動:

1. host の小文字化・既定ポートの省略などは pydantic の正規化に従う
2. tracking parameters を除去 (utm_* / fbclid / gclid / dclid / msclkid /
   mc_cid / mc_eid / ref / ref_src / referrer)
3. path 末尾の ``/`` を除去 (root path ``/`` は保持)
4. fragment (``#...``) を除去
5. scheme は保存 (http と https は別 URL として扱う)
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode

from pydantic import AnyHttpUrl

from app.collection.article_acquisition.tools.url_normalizer import _TRACKING_PARAMS


def canonicalize_url(url: str) -> str:
    """canonicalize 済み URL を返す。

    ``analyzable_articles.source_url`` / ``incomplete_articles.url`` 共通の正規化。
    冪等: ``canonicalize_url(canonicalize_url(x)) == canonicalize_url(x)``。
    入力は http(s) URL であること (解析できなければ pydantic の
    ``ValidationError`` を送出する)。
    """
    parsed = AnyHttpUrl(url)

    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/") or "/"

    pairs = parse_qsl(parsed.query or "", keep_blank_values=True)
    filtered = [(k, v) for k, v in pairs if k.lower() not in _TRACKING_PARAMS]

    return str(
        AnyHttpUrl.build(
            scheme=parsed.scheme,
            username=parsed.username,
            password=parsed.password,
            host=parsed.host or "",
            port=parsed.port,
            # build は path の先頭に / を補うため、重複しないよう1つ外して渡す。
            path=path[1:],
            query=urlencode(filtered, doseq=True) or None,
        )
    )
