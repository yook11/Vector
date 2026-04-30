"""URL 正規化道具 — tracking parameter (utm_*, gclid 等) の除去のみを担う。

collection-acquisition-redesign Phase 0c。

スコープを意図的に絞る:

- ✅ 除去: tracking parameter (UTM 系 + 広告クリック ID + Mailchimp 系 + ref)
- ❌ 触らない: scheme, host, path, fragment, www の有無, trailing slash の有無

理由 (`spec collection-acquisition-redesign.md §5.1`): scheme / host / path の
正規化はソース毎に挙動が異なる (例: TechCrunch は trailing slash 必須、HN は
amp.example.com を例外的に保持) ため、共通道具では tracking parameter のみを
扱い、その他は各 Fetcher の責務に委ねる。
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.shared.value_objects.safe_url import SafeUrl

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

    クエリ全体を除去せず、tracking parameter として識別された key のみを
    捨てる: 残りのクエリ (記事 ID 等の必須パラメータ) は順序を保って
    保持される。fragment / www / trailing slash には触らない。
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
