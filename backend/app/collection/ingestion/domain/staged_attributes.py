"""``pending_html_articles.staged_attributes`` (JSONB) の Pydantic 型定義。

PR2.5-A 新設。Stage 1 で取れた、Stage 2 で HTML 本文と merge して articles を
完成させる材料 (title / published_at 等) を JSONB として永続化するための
schema。ソース種別 (RSS / sitemap-only / HTML listing / API) によって取れる
field が異なるため、すべて optional とする。

進化ルール (spec ``pipeline-events-stage2-design.md`` §staged_attributes):

- field 追加 — optional + default=None で後方互換に追加 OK
- field 削除 — deprecated 期間を経て削除 (老朽 closed 行が drain した後)
- field rename — Pydantic alias で旧名を受ける期間を設ける、または
  ``jsonb_set`` で migration
- 型変更 (互換性なし) — 別 field を追加して移行 (in-place 変更は NG)
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class StagedArticleAttributes(BaseModel):
    """Stage 2 で HTML 本文と merge する Stage 1 由来の partial metadata。

    ``model_config``:
    - ``extra="forbid"`` — 想定外 field の書込を validation で弾く (writer 側 typo 検知)
    - ``frozen=True`` — immutability、JSONB に焼き付けた後の変更を防ぐ意図と一致
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str | None = None
    published_at: datetime | None = None
