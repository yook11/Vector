"""External Query Agent のDeepSeek output binding。"""

from typing import Final

from app.agent.runtime.deepseek import DeepSeekOutputBinding

EXTERNAL_QUERY_DEEPSEEK_BINDING: Final[DeepSeekOutputBinding] = DeepSeekOutputBinding(
    function_name="generate_search_queries",
    description="Return the declared external query draft.",
)
