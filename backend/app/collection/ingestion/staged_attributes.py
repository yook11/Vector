"""``pending_html_articles.staged_attributes`` JSONB の structured 型。

Pattern H 1 段目 (``IngestionService``) で RSS 由来の補完情報を
2 段目 (``ContentFetchService``) に DB 経由で渡すための frozen な値型。

- ``title``: RSS 由来の title。``prefer_html_title=True`` のときのみ
  HTML 由来 title に置換される (sitemap 系ソース対応)。
- ``published_at_hint``: RSS 由来の発行日。HTML から取れる published_at より
  常に優先される (caller 側 merge 規則は ``ReadyForArticle.try_advance_from``)。
- ``prefer_html_title``: sitemap 系のように RSS が title を持たない / HTML 側が
  正本のソースで使う opt-in 旗。

DB 保存形式は ``model_dump(mode="json")`` で datetime → ISO 文字列化。読出は
``model_validate(jsonb_dict)`` で復元する。``pending_html_articles.staged_attributes``
は不変条件をアプリ層で保持 (DB CHECK で内部構造は強制しない)。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.collection.extraction.domain.value_objects import PublishedAt

_TITLE_MIN_LENGTH = 1
_TITLE_MAX_LENGTH = 500


class StagedArticleAttributes(BaseModel):
    """Pattern H 補完待ち情報の DB 保存用 frozen Model。"""

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=_TITLE_MIN_LENGTH, max_length=_TITLE_MAX_LENGTH)
    published_at_hint: PublishedAt | None = None
    prefer_html_title: bool = False
