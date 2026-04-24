"""Terminal streaming display for Claude Agent SDK messages."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.text import Text

from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

AGENT_COLORS: dict[str, str] = {
    "main": "white",
    "planner": "cyan",
    "domain_expert": "blue",
    "backend": "magenta",
    "security": "red",
    "sre": "yellow",
    "frontend": "bright_cyan",
    "synthesizer": "green",
}


class Stream:
    """SDK メッセージを色分けして表示する薄いラッパー。"""

    def __init__(self) -> None:
        self.console = Console()

    def section(self, title: str, color: str = "white") -> None:
        self.console.print()
        self.console.rule(f"[bold {color}]{title}[/]")

    def info(self, text: str) -> None:
        self.console.print(f"[dim]{text}[/]")

    def print_message(self, msg: Any, current_agent: str = "main") -> None:
        color = AGENT_COLORS.get(current_agent, "white")
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    self.console.print(Text(block.text, style=color))
                elif isinstance(block, ToolUseBlock):
                    preview = self._preview_tool_input(block.input)
                    self.console.print(
                        f"[dim italic]  -> {block.name}({preview})[/]"
                    )
        elif isinstance(msg, ResultMessage):
            cost = msg.total_cost_usd or 0.0
            self.console.print(
                f"[dim]  [result] duration={msg.duration_ms}ms "
                f"turns={msg.num_turns} cost=${cost:.4f}[/]"
            )
            if msg.is_error:
                self.console.print(f"[bold red]  [error] {msg.result}[/]")

    @staticmethod
    def _preview_tool_input(data: dict[str, Any]) -> str:
        """tool_use の input を1行に要約。"""
        if not isinstance(data, dict):
            return ""
        keys_of_interest = ("file_path", "path", "pattern", "command")
        for key in keys_of_interest:
            if key in data:
                value = str(data[key])
                return f"{key}={value[:80]}"
        return ", ".join(list(data.keys())[:3])
