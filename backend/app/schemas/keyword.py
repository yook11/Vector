import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from app.schemas.category import CategoryBrief

# --- XSS対策: キーワードのホワイトリスト ---
# キーワードは検索や一覧表示に使われるテキスト。
# HTMLタグに使われる < > " ' 等を排除する。
#
# 許可する文字:
#   \w (re.UNICODE): Unicode文字 + 英数字 + アンダースコア
#   スペース: 半角スペースのみ（\s ではなく " " で限定）
#   ハイフン、ドット、&、/、+、#: キーワードで自然に使われる記号
#     例: "AI/ML", "AT&T", "C++", "C#", "Node.js"
# (?=.*\w): 少なくとも1文字の\wを含むことを要求
_KEYWORD_RE = re.compile(r"^(?=.*\w)[\w \-\.&/+#]+$", re.UNICODE)


class KeywordCreate(BaseModel):
    """POST /api/v1/keywords request body."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    keyword: str = Field(min_length=1, max_length=200)
    category_ids: list[int] = []

    @field_validator("keyword", mode="before")
    @classmethod
    def strip_keyword(cls, v: object) -> object:
        """Strip whitespace before length validation.

        mode="before" receives raw input (any type), so we guard with isinstance.
        """
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("keyword", mode="after")
    @classmethod
    def validate_keyword_chars(cls, v: str) -> str:
        if not _KEYWORD_RE.match(v):
            raise ValueError(
                "Keyword can only contain letters, numbers, spaces, "
                "hyphens, dots, &, /, +, #, and underscores"
            )
        return v


class KeywordUpdate(BaseModel):
    """PATCH /api/v1/keywords/{id} request body."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    category_ids: list[int] | None = None


class KeywordResponse(BaseModel):
    """Keyword in API responses (list, detail, embedded in news)."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    keyword: str
    categories: list[CategoryBrief] = []
    article_count: int = 0
    created_at: datetime


class KeywordListResponse(BaseModel):
    """GET /api/v1/keywords response wrapper."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    items: list[KeywordResponse]


class KeywordBrief(BaseModel):
    """Minimal keyword info embedded in NewsResponse."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    keyword: str
    categories: list[CategoryBrief] = []
