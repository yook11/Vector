"""DeepSeek-V4-Flash + Function Calling + strict mode (beta) の PoC.

Vector の ``ClassificationRawResponse`` を strict 互換に変換した schema で
実際に分類が動くかを検証する。Phase 0 の疎通確認専用で、PR-A 着手時に削除する。

実行:
    set -a && source /Users/you/Vector/.env && set +a
    cd /Users/you/Vector/backend && uv run --with openai python scripts/poc_deepseek_classifier.py

検証する 4 ステップ:
1. Pydantic が生成する素のスキーマ
2. strict 互換に変換した schema (minLength/maxLength 除去 + additionalProperties: false)
3. 素のスキーマで strict mode 呼び出し → server-side validation で fail することを確認
4. strict 互換 schema で実分類 → tool_calls.arguments を ``ClassificationRawResponse``
   で再検証できることを確認
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openai import OpenAI

from app.analysis.classifier.schema import ClassificationRawResponse


SAMPLES = [
    {
        "title_ja": "OpenAI、新型 GPT-6 の API 提供開始",
        "summary_ja": (
            "OpenAI は新型大規模言語モデル GPT-6 を正式リリースした。"
            "コーディング能力と推論精度が前世代比で大幅に向上したとされる。"
        ),
    },
    {
        "title_ja": "東京で桜祭り開催、観光客 100 万人",
        "summary_ja": (
            "上野公園で開催された桜祭りに 100 万人の観光客が訪れた。"
            "屋台や伝統舞踊など多彩なイベントが行われた。"
        ),
    },
]


def strip_unsupported(node: Any) -> None:
    """strict mode 未対応の制約を再帰的に除去する."""
    if isinstance(node, dict):
        for key in ("minLength", "maxLength", "minItems", "maxItems"):
            node.pop(key, None)
        for value in node.values():
            strip_unsupported(value)
    elif isinstance(node, list):
        for item in node:
            strip_unsupported(item)


def add_additional_properties_false(node: Any) -> None:
    """object 型に additionalProperties: false を再帰的に付与する."""
    if isinstance(node, dict):
        if node.get("type") == "object" and "additionalProperties" not in node:
            node["additionalProperties"] = False
        for value in node.values():
            add_additional_properties_false(value)
    elif isinstance(node, list):
        for item in node:
            add_additional_properties_false(item)


def build_strict_schema() -> dict[str, Any]:
    schema = ClassificationRawResponse.model_json_schema()
    strip_unsupported(schema)
    add_additional_properties_false(schema)
    return schema


def build_inline_strict_schema() -> dict[str, Any]:
    """$ref を inline 展開し、enum/pattern を properties に直接埋め込む.

    DeepSeek strict mode が ``$ref`` を解釈しているのか、
    それとも enum/pattern 自体を enforce しないのかを切り分ける。
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["category", "topic", "investor_take"],
        "properties": {
            "category": {
                "type": "string",
                "enum": [
                    "ai", "bio", "computing", "energy", "materials",
                    "mobility", "network", "robotics", "security",
                    "semiconductor", "space", "out_of_scope",
                ],
                "description": "Vector の 11 カテゴリのいずれか、または out_of_scope",
            },
            "topic": {
                "type": "string",
                "pattern": "^[a-z0-9]+( [a-z0-9]+)*$",
                "description": (
                    "正規化済み英語小文字 1-3 語のラベル。例: 'ai agents', "
                    "'quantum computing', '6g'。日本語不可、大文字不可、"
                    "ハイフン/アンダースコア不可、冠詞 (a/an/the/in/of) 不可"
                ),
            },
            "investor_take": {
                "type": "string",
                "description": "日本語の投資家向け論評（短文、空文字不可）",
            },
        },
    }


def call_deepseek(client: OpenAI, schema: dict[str, Any], sample: dict[str, str]) -> dict[str, Any]:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "classify_article",
                "strict": True,
                "description": "記事を Vector の 11 カテゴリのいずれか、または OUT_OF_SCOPE に分類する",
                "parameters": schema,
            },
        }
    ]
    user_prompt = (
        "次の記事を分類してください。\n"
        f"タイトル: {sample['title_ja']}\n"
        f"要約: {sample['summary_ja']}"
    )
    resp = client.chat.completions.create(
        model="deepseek-v4-flash",
        messages=[{"role": "user", "content": user_prompt}],
        tools=tools,
        tool_choice={"type": "function", "function": {"name": "classify_article"}},
        max_tokens=512,
        extra_body={"thinking": {"type": "disabled"}},
    )
    choice = resp.choices[0]
    tool_calls = choice.message.tool_calls or []
    return {
        "finish_reason": choice.finish_reason,
        "reasoning_content": getattr(choice.message, "reasoning_content", None),
        "tool_calls": [
            {"name": tc.function.name, "arguments": tc.function.arguments}
            for tc in tool_calls
        ],
        "usage": resp.usage.model_dump() if resp.usage else None,
    }


def main() -> int:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("ERROR: DEEPSEEK_API_KEY is not set", file=sys.stderr)
        return 1

    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com/beta")

    print("=" * 60)
    print("Step 1: Pydantic-generated raw schema")
    print("=" * 60)
    raw_schema = ClassificationRawResponse.model_json_schema()
    print(json.dumps(raw_schema, indent=2, ensure_ascii=False))

    print("\n" + "=" * 60)
    print("Step 2: strict-compatible schema")
    print("=" * 60)
    strict_schema = build_strict_schema()
    print(json.dumps(strict_schema, indent=2, ensure_ascii=False))

    print("\n" + "=" * 60)
    print("Step 3: raw schema with strict mode (expect fail)")
    print("=" * 60)
    try:
        resp = call_deepseek(client, raw_schema, SAMPLES[0])
        print("UNEXPECTED PASS:")
        print(json.dumps(resp, indent=2, ensure_ascii=False))
    except Exception as exc:
        print(f"EXPECTED FAIL: {type(exc).__name__}: {exc}")

    print("\n" + "=" * 60)
    print("Step 4: strict-compatible schema actual classification")
    print("=" * 60)
    for i, sample in enumerate(SAMPLES, 1):
        print(f"\n--- Sample {i}: {sample['title_ja']} ---")
        try:
            resp = call_deepseek(client, strict_schema, sample)
            print(json.dumps(resp, indent=2, ensure_ascii=False))

            if resp["tool_calls"]:
                args_str = resp["tool_calls"][0]["arguments"]
                try:
                    parsed = ClassificationRawResponse.model_validate_json(args_str)
                    print(f"PYDANTIC VALIDATION OK: {parsed!r}")
                except Exception as exc:
                    print(f"PYDANTIC VALIDATION FAIL: {type(exc).__name__}: {exc}")
        except Exception as exc:
            print(f"DEEPSEEK FAIL: {type(exc).__name__}: {exc}")

    print("\n" + "=" * 60)
    print("Step 5: inline schema (enum/pattern を properties に直接埋め込む)")
    print("=" * 60)
    inline_schema = build_inline_strict_schema()
    print(json.dumps(inline_schema, indent=2, ensure_ascii=False))
    for i, sample in enumerate(SAMPLES, 1):
        print(f"\n--- Sample {i}: {sample['title_ja']} ---")
        try:
            resp = call_deepseek(client, inline_schema, sample)
            print(json.dumps(resp, indent=2, ensure_ascii=False))

            if resp["tool_calls"]:
                args_str = resp["tool_calls"][0]["arguments"]
                try:
                    parsed = ClassificationRawResponse.model_validate_json(args_str)
                    print(f"PYDANTIC VALIDATION OK: {parsed!r}")
                except Exception as exc:
                    print(f"PYDANTIC VALIDATION FAIL: {type(exc).__name__}: {exc}")
        except Exception as exc:
            print(f"DEEPSEEK FAIL: {type(exc).__name__}: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
