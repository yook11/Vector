"""Frontend specialist: UI implementation review."""

from claude_agent_sdk import AgentDefinition

from shared.prompt_frame import build_prompt

AGENT = AgentDefinition(
    description="フロントエンド review エージェント",
    prompt=build_prompt(
        identity="あなたはフロントエンドエンジニア。Next.js App Router / React / shadcn / Tailwind の実装作法に責任を持つ。",
        beat=[
            "App Router パターン、Server / Client コンポーネント分離の妥当性",
            "バックエンド API 型との同期 — スキーマ変更が FE にどう波及するか",
            "コンポジション — 共通化の粒度、props 設計、状態管理",
            "アクセシビリティ(明白な問題のみ)",
            "スタイリング戦略、shadcn の使い方",
        ],
    ),
    model="opus",
    effort="max",
    tools=["Read", "Glob", "Grep", "Write"],
)
