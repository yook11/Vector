"""Search BC 専用 query embedder。Stage 5 (document 永続化) と独立。

Stage 5 (``app/analysis/embedding/``) と Search が解いている問題は別:
- Stage 5: document 単位の永続化、VO 詰め替えあり、audit / retry / quota の対象
- Search: 検索 query 1 回ごとの一時計算、raw ``list[float]`` を Redis cache に
  乗せる、per-user 1 日 quota は router で先に消費される

実装上は ``BaseEmbedder`` と似た形になるが、両者を共用すると一方の変更が他方に
波及する暗黙の結合を作るので独立 hierarchy を維持する
(memory `feedback_no_share_different_problems`)。
"""

from app.search.embedding.base import QueryEmbedder

__all__ = ["QueryEmbedder"]
