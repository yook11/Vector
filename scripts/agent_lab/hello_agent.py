"""SDK smoke test.

Usage:
    uv run hello_agent.py

Authentication:
    - If ANTHROPIC_API_KEY is set in env, it is used (API billing).
    - Otherwise falls back to the Claude Code CLI session (Max/Pro subscription).
"""

import asyncio
import os

from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock


async def main() -> None:
    auth_hint = (
        "ANTHROPIC_API_KEY (API billing)"
        if os.getenv("ANTHROPIC_API_KEY")
        else "Claude Code CLI session (subscription)"
    )
    print(f"[auth] Using: {auth_hint}")

    options = ClaudeAgentOptions(model="sonnet", max_turns=1)

    async for msg in query(
        prompt="Say hello in one short English sentence.",
        options=options,
    ):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    print(f"[assistant] {block.text}")
        elif isinstance(msg, ResultMessage):
            details = [f"duration={msg.duration_ms}ms", f"turns={msg.num_turns}"]
            if msg.total_cost_usd is not None:
                details.append(f"cost=${msg.total_cost_usd:.6f}")
            print(f"[result] {' '.join(details)}")
            if msg.is_error:
                print(f"[error] {msg.result}")


if __name__ == "__main__":
    asyncio.run(main())
