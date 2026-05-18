"""ingestion BC の道具箱 — 各 Fetcher が組み合わせて使う部品群。

collection-acquisition-redesign Phase 0c。共通フローを Template Method で
強制する旧 ``BaseRssFetcher`` を捨て、再利用可能な部品 (``RssParser`` /
``normalize_article_url`` / ``html_to_plain_text``)
として独立させる。各 Fetcher は必要な部品のみを composition で組み立てる
(`spec collection-acquisition-redesign.md §4`).
"""
