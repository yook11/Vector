from sqlalchemy import Column, ForeignKey, Integer, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


class KeywordCategory(SQLModel, table=True):
    __tablename__ = "keyword_categories"

    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(max_length=50, unique=True, nullable=False, index=True)

    # Relationships
    translations: list["KeywordCategoryTranslation"] = Relationship(
        back_populates="category"
    )
    keyword_links: list["KeywordCategoryLink"] = Relationship(back_populates="category")


class KeywordCategoryTranslation(SQLModel, table=True):
    __tablename__ = "keyword_category_translations"
    __table_args__ = (
        UniqueConstraint("category_id", "locale", name="uq_keyword_cat_locale"),
    )

    id: int | None = Field(default=None, primary_key=True)
    category_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("keyword_categories.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    locale: str = Field(max_length=10, nullable=False)
    name: str = Field(max_length=100, nullable=False)

    # Relationships
    category: KeywordCategory = Relationship(back_populates="translations")


class KeywordCategoryLink(SQLModel, table=True):
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
            ForeignKey("keyword_categories.id", ondelete="CASCADE"),
            nullable=False,
        )
    )

    # Relationships
    keyword: "Keyword" = Relationship(back_populates="category_links")
    category: KeywordCategory = Relationship(back_populates="keyword_links")


# Resolve forward references
from app.models.keyword import Keyword  # noqa: E402, F811

KeywordCategory.model_rebuild()
KeywordCategoryLink.model_rebuild()
