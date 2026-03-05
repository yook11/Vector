from datetime import datetime

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.schemas.category import CategoryBrief


class AIModelBrief(BaseModel):
    """Brief AI model info embedded in AnalysisResponse."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    id: int
    provider: str
    name: str


class AnalysisResponse(BaseModel):
    """AI analysis result embedded in NewsResponse."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    title: str
    summary: str
    sentiment: str
    impact_score: int
    reasoning: str | None = None
    ai_model: AIModelBrief
    analyzed_at: datetime
    investment_categories: list[CategoryBrief] = []
