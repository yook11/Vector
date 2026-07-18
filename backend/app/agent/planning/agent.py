"""Question Planner Agent の宣言。"""

from __future__ import annotations

from app.agent.agent import Agent, AgentPrompt, ModelSettings, ModelTarget
from app.agent.planning.ai.schema_tool import QUESTION_PLANNER_GEMINI_SCHEMA
from app.agent.planning.contract import PlanningAttemptInput, QuestionPlanDraft
from app.agent.planning.prompts import (
    PLANNER_INSTRUCTIONS,
    PLANNER_PROMPT_VERSION,
    render_planning_input,
)

QUESTION_PLANNER_PROMPT = AgentPrompt[PlanningAttemptInput](
    version=PLANNER_PROMPT_VERSION,
    instructions=PLANNER_INSTRUCTIONS,
    input_renderer=render_planning_input,
)

QUESTION_PLANNER_AGENT: Agent[PlanningAttemptInput, QuestionPlanDraft] = Agent(
    name="question_planner",
    prompt=QUESTION_PLANNER_PROMPT,
    model=ModelTarget(provider="gemini", name="gemini-2.5-flash-lite"),
    model_settings=ModelSettings(temperature=0.1, max_output_tokens=1024),
    output_type=QuestionPlanDraft,
    response_schema=QUESTION_PLANNER_GEMINI_SCHEMA,
)
