"""複数 feed source (NASA / Cornell) の ``select`` で使う URL dedup helper。

1 記事が複数 feed/category に tag され feed 横断で URL 重複が起きるため、
``select`` 段で非空 link の重複を除く。**空 link は dedup 対象外**で全て残し、
converter の ``MISSING_URL`` 監査経路へ届ける (implicit drop 禁止 =
failure-visibility)。
"""

from __future__ import annotations

from app.collection.article_acquisition.reader.rss_reader import RssEntry


def dedup_by_link(entries: list[RssEntry]) -> list[RssEntry]:
    """非空 link の重複を除く (出現順を保つ)。空 link は全て残す。"""
    seen: set[str] = set()
    result: list[RssEntry] = []
    for entry in entries:
        if entry.link and entry.link in seen:
            continue
        if entry.link:
            seen.add(entry.link)
        result.append(entry)
    return result
