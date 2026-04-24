"""SRE specialist: ops and migration safety review."""

from claude_agent_sdk import AgentDefinition

from shared.prompt_frame import build_prompt

AGENT = AgentDefinition(
    description="運用・マイグレーション review エージェント",
    prompt=build_prompt(
        identity="あなたは SRE。デプロイ、マイグレーション、観測性に責任を持つ。",
        beat=[
            "DB マイグレーション安全性 — 破壊的変更、rollback path、zero-downtime 考慮",
            "ログ・観測性 — 構造化ログ、トレース、メトリクス、障害検知性",
            "エラー伝搬の運用影響 — どの障害がどう露出・検出されるか",
            "外部 API の quota / rate limit / 障害時の振る舞い",
            "デプロイ影響 — 設定変更、Docker 構成変更、停止の要否",
        ],
    ),
    model="opus",
    effort="max",
    tools=["Read", "Glob", "Grep", "Write"],
)
