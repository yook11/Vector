"""Article の title / body 長さ境界 — collection BC の SSoT。

- ``ARTICLE_TITLE_MIN_LENGTH`` (=1): 非空保証の下限。
- ``ARTICLE_TITLE_MAX_LENGTH`` (=500): DB CHECK / 抽出器の整形上限。
- ``ARTICLE_BODY_MIN_LENGTH`` (=50): 抽出器の品質ゲート閾値
  (未満は ``ExtractionEmpty``)。
- ``ARTICLE_BODY_MAX_LENGTH`` (=1 MiB): DoS 上限。日本語を考慮しても十分。

consumer (``analyzable_article`` / ``observed_article`` /
``article_completion.extractor`` / ``fetchers.tools.passport_builder``) は
全てここから import し、リテラルの二重化 (drift) を構造的に排除する。
"""

from __future__ import annotations

ARTICLE_TITLE_MIN_LENGTH = 1
ARTICLE_TITLE_MAX_LENGTH = 500
ARTICLE_BODY_MIN_LENGTH = 50
ARTICLE_BODY_MAX_LENGTH = 1_048_576
