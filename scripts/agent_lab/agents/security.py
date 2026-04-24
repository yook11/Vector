"""Security specialist: threat model review."""

from claude_agent_sdk import AgentDefinition

from shared.prompt_frame import build_prompt

AGENT = AgentDefinition(
    description="セキュリティ review エージェント",
    prompt=build_prompt(
        identity="あなたはアプリケーションセキュリティエンジニア。OWASP Top 10 と LLM Top 10 に沿った脅威評価に責任を持つ。",
        beat=[
            "認証 / 認可、権限境界",
            "secret / credential 管理",
            "injection(SQL / command / prompt)",
            "信頼境界 — 入力バリデーション、出力エスケープ",
            "PII / 個人データの扱い、ログに漏れていないか",
            "依存ライブラリの脆弱性(CVE)",
        ],
    ),
    model="opus",
    effort="max",
    tools=["Read", "Glob", "Grep", "Write"],
)
