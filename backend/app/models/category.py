from sqlalchemy import Column, ForeignKey, Integer, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


class Category(SQLModel, table=True):
    __tablename__ = "categories"

    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(max_length=50, unique=True, nullable=False, index=True)
    name: str = Field(max_length=50, unique=True, nullable=False)

    # Relationships
    keyword_links: list["KeywordCategoryLink"] = Relationship(back_populates="category")


class KeywordCategoryLink(SQLModel, table=True):
    """M:N link between keywords and categories. Will be removed in Phase 2
    when Keyword gets a direct category_id FK."""

    __tablename__ = "keyword_category_links"
    __table_args__ = (
        UniqueConstraint("keyword_id", "category_id", name="uq_keyword_category"),
    )

    id: int | None = Field(default=None, primary_key=True)
    keyword_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("keywords.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    category_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("categories.id", ondelete="CASCADE"),
            nullable=False,
        )
    )

    # Relationships
    keyword: "Keyword" = Relationship(back_populates="category_links")
    category: Category = Relationship(back_populates="keyword_links")


# Resolve forward references
from app.models.keyword import Keyword  # noqa: E402, F811

Category.model_rebuild()
KeywordCategoryLink.model_rebuild()
