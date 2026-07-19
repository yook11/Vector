"""Question Context Agentの宣言。"""

from __future__ import annotations

from app.agent.agent import Agent, AgentPrompt, ModelSettings, ModelTarget
from app.agent.question_context.ai.schema_tool import QUESTION_CONTEXT_GEMINI_SCHEMA
from app.agent.question_context.contract import (
    QuestionContextDraft,
    QuestionContextGenerationInput,
)
from app.agent.question_context.prompts import (
    QUESTION_CONTEXT_INSTRUCTIONS,
    QUESTION_CONTEXT_PROMPT_VERSION,
    render_question_context_input,
)

QUESTION_CONTEXT_PROMPT = AgentPrompt[QuestionContextGenerationInput](
    version=QUESTION_CONTEXT_PROMPT_VERSION,
    instructions=QUESTION_CONTEXT_INSTRUCTIONS,
    input_renderer=render_question_context_input,
)

QUESTION_CONTEXT_AGENT: Agent[
    QuestionContextGenerationInput,
    QuestionContextDraft,
] = Agent(
    name="question_context",
    prompt=QUESTION_CONTEXT_PROMPT,
    model=ModelTarget(provider="gemini", name="gemini-2.5-flash-lite"),
    model_settings=ModelSettings(temperature=0.1, max_output_tokens=1024),
    output_type=QuestionContextDraft,
    response_schema=QUESTION_CONTEXT_GEMINI_SCHEMA,
)
