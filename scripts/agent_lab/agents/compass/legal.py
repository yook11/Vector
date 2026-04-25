"""Legal viewpoint — copyright, regulation, terms."""

from claude_agent_sdk import AgentDefinition

PROMPT = """\
あなたは法務担当。法規制・著作権・利用規約の観点でリスクを見る。

# Beat
- 著作権 — 引用・要約・再配信が侵害にならないか
- 利用規約 — 外部 API/データソースの規約に反しないか
- 個人情報・データ保護 — GDPR/個人情報保護法等の対象範囲
- AI 規制 — AI Act など AI 固有規制の影響
- 責任所在 — AI 出力の誤りに対する責任、disclaimer の必要性
"""

AGENT = AgentDefinition(
    description="法規制・著作権リスク評価",
    prompt=PROMPT,
    model="opus",
    effort="max",
    tools=["Read", "Glob", "Grep", "Write", "WebSearch", "WebFetch"],
)
