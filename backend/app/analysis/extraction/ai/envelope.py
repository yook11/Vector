"""``ExtractionCall`` — extractor 戻り値の envelope。

PR3-a-1 で ``BaseExtractor.extract()`` の戻り値型を ``ExtractionResult`` から
本 envelope に変更する。Service 層が **AI raw 応答** を audit 焼付できる
ようにするため、``raw_response`` と ``prompt_version`` を一緒に運ぶ。

raw_response は extraction 監査の S 級情報 (Vector のどこにも残らない極めて
貴重なデバッグ情報、`docs/observability/pipeline-events-design.md` 参照)。
prompt_version は ADR §prompt_version の規律で確定する 8 文字 hash で、
extractor 自身が宣言した値を Service にそのまま伝搬する。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.analysis.extraction.domain import ExtractionResult


@dataclass(frozen=True, slots=True)
class ExtractionCall:
    """extractor の 1 回の API call の結果。

    Attributes:
        result: 構造化された抽出結果 (Pydantic model)。
        raw_response: SDK が返した text 応答 (audit に焼付ける、2KB 程度上限想定)。
        prompt_version: 呼び出し元 Prompt class の VERSION (8 文字 hash)。
    """

    result: ExtractionResult
    raw_response: str
    prompt_version: str
