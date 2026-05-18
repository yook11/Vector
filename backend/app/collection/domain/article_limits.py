"""Article の title / body 長さ境界 — collection BC の SSoT。

- ``ARTICLE_TITLE_MIN_LENGTH`` (=1): 非空保証の下限。
- ``ARTICLE_TITLE_MAX_LENGTH`` (=500): DB CHECK / 抽出器の整形上限。
- ``ARTICLE_BODY_MIN_LENGTH`` (=50): 抽出器の品質ゲート閾値。
- ``ARTICLE_BODY_MAX_LENGTH`` (=1 MiB): DoS 上限。

consumer は全てここから import し、リテラルの二重化を避ける。
"""

from __future__ import annotations

ARTICLE_TITLE_MIN_LENGTH = 1
ARTICLE_TITLE_MAX_LENGTH = 500
ARTICLE_BODY_MIN_LENGTH = 50
ARTICLE_BODY_MAX_LENGTH = 1_048_576
