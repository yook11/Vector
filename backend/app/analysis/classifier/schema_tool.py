"""DeepSeek strict mode 用の AI 境界 JSON Schema 定数。

DeepSeek の Function Calling + ``strict: true`` (beta endpoint) は ``$ref``/``$defs``
経由の制約を enforce しないため、Pydantic の ``model_json_schema()`` 出力をそのまま
渡せない (specs/stage2-deepseek-migration.md の PoC 結果参照)。

このモジュールは AI に渡す inline flat な strict 互換 JSON Schema を手書き定数として
保持する。受信後の検証は ``ClassificationRawResponse.model_validate_json()`` で
行うため、subset 外制約 (``minLength``/``maxLength`` 等) はここに書かない。

整合性ドリフト (enum 追加忘れ等) は ``test_classification_tool_schema.py`` で
構造的に検出する。
"""

from __future__ import annotations

from typing import Any

from app.analysis.classifier.schema import ValidCategory

CLASSIFICATION_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["category", "topic", "investor_take"],
    "properties": {
        "category": {
            "type": "string",
            "enum": [c.value for c in ValidCategory],
            "description": "Vector の 11 カテゴリのいずれか、または out_of_scope",
        },
        "topic": {
            "type": "string",
            "pattern": r"^[a-z0-9]+( [a-z0-9]+)*$",
            "description": (
                "正規化済み英語小文字 1-3 語のラベル。例: 'ai agents'、"
                "'quantum computing'、'6g'。日本語不可、大文字不可、"
                "ハイフン/アンダースコア不可、冠詞 (a/an/the/in/of) 不可"
            ),
        },
        "investor_take": {
            "type": "string",
            "description": "日本語の投資家向け論評（短文、空文字不可）",
        },
    },
}
