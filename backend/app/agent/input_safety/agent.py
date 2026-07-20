"""Input Safety Agentの宣言。"""

from __future__ import annotations

from app.agent.agent import Agent, AgentPrompt, ModelSettings, ModelTarget
from app.agent.input_safety.ai.schema_tool import INPUT_SAFETY_GEMINI_SCHEMA
from app.agent.input_safety.contract import (
    InputSafetyAgentInput,
    InputSafetyAgentOutput,
)
from app.agent.input_safety.prompts import (
    INPUT_SAFETY_INSTRUCTIONS,
    INPUT_SAFETY_PROMPT_VERSION,
    render_input_safety_input,
)

INPUT_SAFETY_PROMPT = AgentPrompt[InputSafetyAgentInput](
    version=INPUT_SAFETY_PROMPT_VERSION,
    instructions=INPUT_SAFETY_INSTRUCTIONS,
    input_renderer=render_input_safety_input,
)

INPUT_SAFETY_AGENT: Agent[
    InputSafetyAgentInput,
    InputSafetyAgentOutput,
] = Agent(
    name="input_safety",
    prompt=INPUT_SAFETY_PROMPT,
    model=ModelTarget(provider="gemini", name="gemini-2.5-flash-lite"),
    model_settings=ModelSettings(temperature=0.0, max_output_tokens=128),
    output_type=InputSafetyAgentOutput,
    response_schema=INPUT_SAFETY_GEMINI_SCHEMA,
)
