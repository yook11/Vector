"""URL 正規化道具 — tracking parameter (utm_*, gclid 等) の除去のみを担う。

scheme / host / path / fragment / www / trailing slash には触らない
(ソース毎に挙動が異なるため共通道具の責務外)。
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.shared.security.safe_url import SafeUrl

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


def normalize_article_url(url: SafeUrl) -> SafeUrl:
    """tracking parameter を除去した ``SafeUrl`` を返す。

    残りのクエリは順序を保って保持する。
    """
    parsed = urlparse(str(url))
    if not parsed.query:
        return url

    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    filtered = [(k, v) for k, v in pairs if k.lower() not in _TRACKING_PARAMS]
    if len(filtered) == len(pairs):
        return url

    new_query = urlencode(filtered, doseq=True)
    rebuilt = urlunparse(parsed._replace(query=new_query))
    return SafeUrl(rebuilt)
