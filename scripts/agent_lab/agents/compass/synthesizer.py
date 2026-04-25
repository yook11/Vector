"""Synthesizer: integrate 10 contributions into a navigable discussion."""

from claude_agent_sdk import AgentDefinition

PROMPT = """\
あなたはファシリテーター。10 視点の contributions を読み、
読み手が認知負荷なく次の判断ができる形に整理する。

# 責務
- 全 contributions を Read で読む
- 視点間の対立と取りうる選択肢を構造化する
- 意見を散らばったまま並べない

# Output
指示されたパスに Markdown で Write する。
"""

AGENT = AgentDefinition(
    description="対立点・選択肢の整理担当",
    prompt=PROMPT,
    model="opus",
    effort="max",
    tools=["Read", "Glob", "Grep", "Write"],
)
