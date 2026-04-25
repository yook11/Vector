"""Security viewpoint."""

from claude_agent_sdk import AgentDefinition

PROMPT = """\
あなたはセキュリティ担当。技術的なセキュリティリスクを見る。

# Beat
- 技術的脆弱性 — 認証/認可/インジェクション等の典型リスク
- データ取扱 — 個人情報・機密情報の保護
- AI 攻撃面 — プロンプトインジェクション、ジェイルブレイク等
- 依存リスク — 使用ライブラリ・モデルの既知の脆弱性
"""

AGENT = AgentDefinition(
    description="セキュリティリスク評価",
    prompt=PROMPT,
    model="opus",
    effort="max",
    tools=["Read", "Glob", "Grep", "Write", "WebSearch", "WebFetch"],
)
