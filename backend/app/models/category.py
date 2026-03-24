from sqlmodel import Field, Relationship, SQLModel


class Category(SQLModel, table=True):
    __tablename__ = "categories"

    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(max_length=50, unique=True, nullable=False, index=True)
    name: str = Field(max_length=50, unique=True, nullable=False)

    # Relationships
    keywords: list["Keyword"] = Relationship(back_populates="category")


# Resolve forward references
from app.models.keyword import Keyword  # noqa: E402, F811

Category.model_rebuild()
