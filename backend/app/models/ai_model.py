from sqlalchemy import UniqueConstraint
from sqlmodel import Field, Relationship, SQLModel


class AIModel(SQLModel, table=True):
    __tablename__ = "ai_models"
    __table_args__ = (
        UniqueConstraint("provider", "name", name="uq_ai_model_provider_name"),
    )

    id: int | None = Field(default=None, primary_key=True)
    provider: str = Field(max_length=20, nullable=False)
    name: str = Field(max_length=50, nullable=False)
    is_active: bool = Field(default=True, nullable=False)

    # Relationships
    analyses: list["AnalysisResult"] = Relationship(back_populates="ai_model")


# Resolve forward references
from app.models.analysis import AnalysisResult  # noqa: E402, F811

AIModel.model_rebuild()
