from sqlalchemy import CheckConstraint
from sqlmodel import Field, Relationship, SQLModel


class Category(SQLModel, table=True):
    __tablename__ = "categories"
    __table_args__ = (
        CheckConstraint(
            "slug ~ '^[a-z0-9][a-z0-9_]{0,49}$'",
            name="ck_categories_slug_format",
        ),
        CheckConstraint(
            "char_length(name) >= 1",
            name="ck_categories_name_not_empty",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(max_length=50, unique=True, nullable=False, index=True)
    name: str = Field(max_length=50, unique=True, nullable=False)

    # Relationships
    keywords: list["Keyword"] = Relationship(back_populates="category")


# Resolve forward references
from app.models.keyword import Keyword  # noqa: E402, F811

Category.model_rebuild()
