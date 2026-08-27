"""Research Handoff Agentの宣言。"""

from __future__ import annotations

from app.agent.agent import Agent, AgentPrompt, ModelSettings, ModelTarget
from app.agent.research_handoff.ai.schema_tool import RESEARCH_HANDOFF_GEMINI_SCHEMA
from app.agent.research_handoff.contract import (
    HandoffOrganizerInput,
    ResearchHandoffDraft,
)
from app.agent.research_handoff.prompts import (
    RESEARCH_HANDOFF_INSTRUCTIONS,
    RESEARCH_HANDOFF_PROMPT_VERSION,
    render_organizer_input,
)

RESEARCH_HANDOFF_PROMPT = AgentPrompt[HandoffOrganizerInput](
    version=RESEARCH_HANDOFF_PROMPT_VERSION,
    instructions=RESEARCH_HANDOFF_INSTRUCTIONS,
    input_renderer=render_organizer_input,
)

RESEARCH_HANDOFF_AGENT: Agent[HandoffOrganizerInput, ResearchHandoffDraft] = Agent(
    name="research_handoff",
    prompt=RESEARCH_HANDOFF_PROMPT,
    model=ModelTarget(provider="gemini", name="gemini-2.5-flash-lite"),
    model_settings=ModelSettings(temperature=0.1, max_output_tokens=1024),
    output_type=ResearchHandoffDraft,
    response_schema=RESEARCH_HANDOFF_GEMINI_SCHEMA,
)
