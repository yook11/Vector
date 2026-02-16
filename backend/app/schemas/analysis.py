from datetime import datetime

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class AnalysisResponse(BaseModel):
    """AI analysis result embedded in NewsResponse."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )

    title_ja: str
    summary_ja: str
    sentiment: str
    impact_score: int
    key_topics: list[str] | None = None
    reasoning: str | None = None
    ai_provider: str
    analyzed_at: datetime
