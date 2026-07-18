"""External Query / Selector Agent のDeepSeek output binding。"""

from typing import Final

from app.agent.runtime.deepseek import DeepSeekOutputBinding

EXTERNAL_QUERY_DEEPSEEK_BINDING: Final[DeepSeekOutputBinding] = DeepSeekOutputBinding(
    function_name="generate_search_queries",
    description="Return the declared external query draft.",
)

EXTERNAL_EVIDENCE_SELECTOR_DEEPSEEK_BINDING: Final[DeepSeekOutputBinding] = (
    DeepSeekOutputBinding(
        function_name="select_evidence",
        description="Return the declared external evidence selection draft.",
    )
)
