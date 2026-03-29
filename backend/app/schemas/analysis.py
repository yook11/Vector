from datetime import datetime

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.models.article_analysis import ImpactLevel


class AnalysisResponse(BaseModel):
    """AI analysis result embedded in NewsResponse."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    translated_title: str
    summary: str
    impact_level: ImpactLevel
    reasoning: str
    ai_model: str
    analyzed_at: datetime
