"""Evidence Reviewer Agent のDeepSeek output binding。"""

from typing import Final

from app.agent.runtime.deepseek import DeepSeekOutputBinding

EVIDENCE_REVIEWER_DEEPSEEK_BINDING: Final[DeepSeekOutputBinding] = (
    DeepSeekOutputBinding(
        function_name="review_evidence",
        description="Return the declared evidence review draft.",
    )
)
