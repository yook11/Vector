"""LLM 呼出条件 5 要素から ``prompt_version`` を構造的に算出する。

ADR (`docs/observability/pipeline-events-design.md`) §prompt_version の規律 を
実装する utility。

5 要素 = ``prompt_template`` / ``model`` / ``gen_config`` /
``response_schema`` / ``system_instruction``。SHA-256 で hash し prefix 8 文字を採る。

呼出は **Spec module の module-level** で 1 回だけ走る (`Final` 定数代入)。
runtime で再計算しないので、`@cache` 不要、外部代入不要。

git short SHA 注入は採らない: プロンプト未変更の commit でも値が変わるノイズが
発生し、`prompt_version 別の OOS 率` 等の SQL 集計が薄まるため。詳細は ADR 参照。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def compute_call_signature(
    *,
    prompt_template: str,
    model: str,
    gen_config: Mapping[str, Any],
    response_schema: Mapping[str, Any] | None,
    system_instruction: str | None,
) -> str:
    """LLM 呼出条件 5 要素のハッシュを返す (SHA-256 prefix 8 文字)。

    ``response_schema`` は **既に dict 化された JSON schema** を受ける。
    Pydantic 経路の caller は ``cls.RESPONSE_SCHEMA.model_json_schema()`` を、
    tool schema 経路の caller は dict をそのまま渡す。``None`` は「schema 制約なし」
    を意味し空文字としてハッシュに混ぜる。

    ``gen_config`` の dict 順序差は ``json.dumps(sort_keys=True)`` で吸収する。
    Pydantic の minor version bump で ``model_json_schema()`` 出力の構造が変わる
    可能性は noise として許容 (ADR §prompt_version の規律)。

    field 間に NULL 区切り (``\\x00``) を入れることで「prompt + model の連結が他の
    組合せと衝突する」preimage 攻撃を構造的に潰す。
    """
    h = hashlib.sha256()
    h.update(prompt_template.encode("utf-8"))
    h.update(b"\x00")
    h.update(model.encode("utf-8"))
    h.update(b"\x00")
    h.update(json.dumps(dict(gen_config), sort_keys=True).encode("utf-8"))
    h.update(b"\x00")
    if response_schema is not None:
        h.update(json.dumps(dict(response_schema), sort_keys=True).encode("utf-8"))
    h.update(b"\x00")
    if system_instruction is not None:
        h.update(system_instruction.encode("utf-8"))
    return h.hexdigest()[:8]
