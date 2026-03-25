from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class AIModel(SQLModel, table=True):
    """Legacy model — kept for DB compatibility. Removed in Step 5."""

    __tablename__ = "ai_models"
    __table_args__ = (
        UniqueConstraint("provider", "name", name="uq_ai_model_provider_name"),
    )

    id: int | None = Field(default=None, primary_key=True)
    provider: str = Field(max_length=20, nullable=False)
    name: str = Field(max_length=50, nullable=False)
    is_active: bool = Field(default=True, nullable=False)
