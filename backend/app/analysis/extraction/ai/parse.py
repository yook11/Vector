"""Stage 3 ACL: Gemini SDK structured response → ``Signal`` | ``Noise``。

``GeminiExtractionResponse`` (Gemini SDK 契約型、``relevance`` 持ち) を受け取り、
``relevance`` 値で ``Signal`` / ``Noise`` ドメイン型に振り分ける。本関数が AI
出力のドメイン境界を 1 箇所に集約する (Stage 4 ``parse_assessment`` と対称)。

``GeminiExtractionResponse`` 側で sanitize / not-empty / dedupe validators が
適用済のため、本関数の責務は型振り分けのみ。フィールドコピーで Pydantic
validator は再走するが、入力時点で正規化済の値なので冪等。

provider 非依存ではない (Gemini 単一 provider 前提)。複数 provider をサポート
するタイミングで ``payload: dict`` を受ける形 (Stage 4 と同) に変える想定。
"""

from __future__ import annotations

from app.analysis.extraction.ai.schema import GeminiExtractionResponse
from app.analysis.extraction.domain import Noise, Signal


def parse_extraction(response: GeminiExtractionResponse) -> Signal | Noise:
    """``GeminiExtractionResponse`` を ``Signal`` / ``Noise`` に振り分ける。

    ``response.relevance`` で振り分け、``GeminiExtractionResponse`` 側 validator
    通過後の正規化済 field をそのまま domain 型に詰める。validator は再走する
    が冪等 (入力が既に sanitize 済)。
    """
    if response.relevance == "noise":
        return Noise(
            title_ja=response.title_ja,
            summary_ja=response.summary_ja,
        )
    return Signal(
        title_ja=response.title_ja,
        summary_ja=response.summary_ja,
    )
