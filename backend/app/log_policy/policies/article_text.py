"""本文を扱う目的が共有する禁止項目。"""

# 本文を扱う目的に限り、記事本文と派生テキストを禁止する。
ARTICLE_TEXT_KEYS = frozenset(
    {
        "body",
        "content",
        "text",
        "html",
        "description",
        "summary",
        "translation",
        "key_points",
        "snippet",
        "answer",
    }
)
