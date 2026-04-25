"""Compass: 2-round multi-viewpoint ideation.

Flow:
    Round 1: 10 specialists contribute in parallel from each viewpoint
    Round 2: synthesizer integrates into DISCUSSION.md

Usage:
    uv run compass.py "題材のテキスト"
    uv run compass.py topic.txt --slug my-feature
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from agents.compass import SPECIALISTS, SYNTHESIZER
from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, query
from shared.compass_dialogue import CompassDialogue, make_slug
from shared.stream import Stream


def _vector_root() -> Path:
    """Vector プロジェクトのルートパス。scripts/agent_lab/ の 2 つ上。"""
    return Path(__file__).resolve().parent.parent.parent


def _default_base() -> Path:
    return _vector_root() / "discussions" / "drafts"


async def _run_subagent_turn(
    agent_name: str,
    agent: AgentDefinition,
    instruction: str,
    stream: Stream,
    max_turns: int = 15,
) -> None:
    """単一のサブエージェントにタスクを依頼してストリームする。"""
    options = ClaudeAgentOptions(
        model="opus",
        agents={agent_name: agent},
        allowed_tools=[
            "Read",
            "Write",
            "Glob",
            "Grep",
            "Agent",
            "WebSearch",
            "WebFetch",
        ],
        permission_mode="acceptEdits",
        max_turns=max_turns,
        cwd=str(_vector_root()),
    )
    prompt = f"Use the {agent_name} agent to do the following:\n\n{instruction}"
    async for msg in query(prompt=prompt, options=options):
        stream.print_message(msg, agent_name)


async def _run_specialist(
    name: str,
    agent: AgentDefinition,
    topic: str,
    dialogue: CompassDialogue,
    stream: Stream,
) -> None:
    output = dialogue.contribution_path(name)
    instruction = (
        f"以下の題材について、自軸からの意見を絶対パス "
        f"{output} に Write してください。\n\n"
        f"=== 題材 ===\n{topic}\n=== ここまで ==="
    )
    await _run_subagent_turn(name, agent, instruction, stream, max_turns=15)


async def run_round_1(dialogue: CompassDialogue, topic: str, stream: Stream) -> None:
    stream.section("Round 1: Specialists contribute (parallel)", "bold white")
    tasks = [
        _run_specialist(name, agent, topic, dialogue, stream)
        for name, agent in SPECIALISTS.items()
    ]
    await asyncio.gather(*tasks)

    missing = [
        name for name in SPECIALISTS if not dialogue.contribution_path(name).exists()
    ]
    if missing:
        stream.info(f"Warning: contributions missing for {missing}")


async def run_round_2(dialogue: CompassDialogue, topic: str, stream: Stream) -> None:
    stream.section("Round 2: Synthesizer integrates", "green")
    contribution_files = "\n".join(
        f"- {dialogue.contribution_path(name)}" for name in SPECIALISTS
    )
    instruction = (
        f"以下のファイル群を Read し、DISCUSSION.md を絶対パス "
        f"{dialogue.final_path} に Write してください。\n\n"
        f"## 題材\n{topic}\n\n"
        f"## contributions\n{contribution_files}\n"
    )
    await _run_subagent_turn(
        "synthesizer", SYNTHESIZER, instruction, stream, max_turns=25
    )

    if not dialogue.final_path.exists():
        raise RuntimeError(f"synthesizer did not write {dialogue.final_path}")


async def run_compass(topic: str, slug: str | None, base_dir: Path) -> None:
    stream = Stream()
    dialogue = CompassDialogue(base_dir, make_slug(slug))
    dialogue.write_topic(topic)

    stream.section(f"Compass — {dialogue.root.name}", "bold white")
    stream.info(f"Topic: {topic[:120]}{'...' if len(topic) > 120 else ''}")
    stream.info(f"Output dir: {dialogue.root}")

    await run_round_1(dialogue, topic, stream)
    await run_round_2(dialogue, topic, stream)

    stream.section("Done", "bold green")
    stream.info(f"Discussion: {dialogue.final_path}")
    stream.info(f"Dialogue log: {dialogue.root}")


def _resolve_topic(raw: str) -> str:
    """引数がファイルパスなら読み込み、そうでなければテキストとして扱う。"""
    try:
        candidate = Path(raw)
        if candidate.exists() and candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    except OSError:
        pass
    return raw


def main() -> None:
    parser = argparse.ArgumentParser(description="Compass — multi-viewpoint ideation")
    parser.add_argument(
        "topic",
        help="題材。テキスト直接指定 or テキストファイルのパス。",
    )
    parser.add_argument(
        "--slug",
        default=None,
        help="出力ディレクトリ名のスラッグ",
    )
    parser.add_argument(
        "--base",
        default=None,
        help=f"出力ベースディレクトリ(デフォルト: {_default_base()})",
    )
    args = parser.parse_args()

    topic = _resolve_topic(args.topic)
    base = Path(args.base).resolve() if args.base else _default_base()
    base.mkdir(parents=True, exist_ok=True)

    asyncio.run(run_compass(topic, args.slug, base))


if __name__ == "__main__":
    main()
