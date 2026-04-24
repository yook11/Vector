"""Backend specialist: implementation craft review."""

from claude_agent_sdk import AgentDefinition

from shared.prompt_frame import build_prompt

AGENT = AgentDefinition(
    description="バックエンド実装 review エージェント",
    prompt=build_prompt(
        identity="あなたはバックエンドエンジニア。Python async / FastAPI / ORM 実装の作法に責任を持つ。テスト文化を重視する。",
        beat=[
            "実装パターン、抽象化レベル、モジュール構造",
            "テスト戦略 — unit / integration の境界、fixture 設計、テスタビリティ",
            "型安全 — 型ヒントの過不足、Optional/Union の扱い",
            "エラー伝搬の構造 — どの層で何を捕まえるか",
            "DB アクセス — N+1、トランザクション境界、session 管理",
            "依存注入、副作用の局所化",
        ],
    ),
    model="opus",
    effort="max",
    tools=["Read", "Glob", "Grep", "Write"],
)
