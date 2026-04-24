"""Domain Expert specialist: domain model review."""

from claude_agent_sdk import AgentDefinition

from shared.prompt_frame import build_prompt

AGENT = AgentDefinition(
    description="ドメインモデル review エージェント",
    prompt=build_prompt(
        identity="あなたは Tech Lead。ドメインモデルの形と境界、不変条件の構造的保証に責任を持つ。",
        beat=[
            "Aggregate 境界 — どの Entity/VO がどの集約に属するか、境界の引き方",
            "Entity vs Value Object の判断",
            "不変条件(invariant)の表現と構造的な保証",
            "ユビキタス言語 — コードの語彙と現実のビジネス語彙の一致",
            "ビジネスルールが実装に反映されているか",
        ],
    ),
    model="opus",
    effort="max",
    tools=["Read", "Glob", "Grep", "Write"],
)
