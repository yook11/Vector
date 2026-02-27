from sqlalchemy import Column, ForeignKey, Integer, Text, UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


class InvestmentCategory(SQLModel, table=True):
    __tablename__ = "investment_categories"

    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(max_length=50, unique=True, nullable=False, index=True)

    # Relationships
    translations: list["InvestmentCategoryTranslation"] = Relationship(
        back_populates="category"
    )
    analysis_links: list["AnalysisInvestmentCategory"] = Relationship(
        back_populates="category"
    )


class InvestmentCategoryTranslation(SQLModel, table=True):
    __tablename__ = "investment_category_translations"
    __table_args__ = (
        UniqueConstraint("category_id", "locale", name="uq_invest_cat_locale"),
    )

    id: int | None = Field(default=None, primary_key=True)
    category_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("investment_categories.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    locale: str = Field(max_length=10, nullable=False)
    name: str = Field(max_length=100, nullable=False)
    description: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    # Relationships
    category: InvestmentCategory = Relationship(back_populates="translations")


class AnalysisInvestmentCategory(SQLModel, table=True):
    __tablename__ = "analysis_investment_categories"
    __table_args__ = (
        UniqueConstraint("analysis_id", "category_id", name="uq_analysis_category"),
    )

    id: int | None = Field(default=None, primary_key=True)
    analysis_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("analyses.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    category_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("investment_categories.id", ondelete="CASCADE"),
            nullable=False,
        )
    )

    # Relationships
    analysis: "AnalysisResult" = Relationship(back_populates="category_links")
    category: InvestmentCategory = Relationship(back_populates="analysis_links")


# Resolve forward references
from app.models.analysis import AnalysisResult  # noqa: E402, F811

AnalysisInvestmentCategory.model_rebuild()
