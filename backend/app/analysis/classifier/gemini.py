"""Gemini 実装の Classifier — Stage 2。"""

from __future__ import annotations

import structlog
from google import genai
from google.genai.errors import APIError, ServerError
from google.genai.types import GenerateContentConfig
from pydantic import ValidationError

from app.analysis.classifier.base import BaseClassifier
from app.analysis.classifier.schema import (
    ClassificationRawResponse,
    ClassificationResponse,
    Classified,
    OutOfScope,
    ValidCategory,
)
from app.analysis.errors import (
    AnalysisDomainError,
    ConfigurationError,
    InvalidInputError,
    NetworkError,
    ProviderError,
    RateLimitError,
    UnclassifiedError,
)
from app.analysis.extraction.domain import Entity
from app.config import settings

logger = structlog.get_logger(__name__)


CLASSIFICATION_PROMPT = """\
あなたは先端技術分野のテックニュース分類の専門家です。

タイトル: {title_ja}

サマリー:
{summary_ja}

エンティティ:
{entities_section}

# Step 0 — out_of_scope を先に判定する
記事の主題が技術サブセクター（LLM、量子コンピューティング、ヒューマノイドロボット、\
固体電池、自動運転、核融合 など）ではなく、資金調達・決算・人事・製品発表・\
機能追加・業界動向のようなビジネスや出来事にある記事、\
または Step 1 の 11 カテゴリのいずれにも明確にフィットしない記事は、\
category=out_of_scope を選び、Step 1 をスキップする。

鉄則: 迷ったら out_of_scope に倒す。無理に既存カテゴリに押し込まない。

# Step 1 — category を決定する
「記事が最終的に何を生み出す領域か（主たる artifact / output）」で分類する。\
使われている技術（手段）ではなく、成果物の領域を見る。

【カテゴリ定義】

[Horizontal — enabling tech]

- ai: AI モデル / エージェント / 研究、AI 研究所、AI 規制・安全性、World Models。\
AI の応用先が別領域にある記事は応用先カテゴリへ飛ばす
- semiconductor: チップ設計、製造プロセス、EUV、パッケージング、AI 推論ハード、\
メモリ、量子チップの製造プロセス
- materials: 新材料発見、MI、物性研究、新製造手法そのもの。\
**新材料を使った完成デバイス（EV・電池・チップ）は応用先カテゴリへ**
- computing: **非古典計算パラダイムのみ** — 量子アルゴリズム・誤り訂正、\
ニューロモーフィック、光、DNA。古典計算（PC・OS・開発ツール・SaaS）、\
AI 推論ハード、量子チップの製造プロセスは含まない
- network: 6G、Open RAN、AI-RAN、SDN、海底ケーブル、DC 間通信、エッジ、\
量子ネットワーキング。**SNS・ウェブアプリ・キャリア料金・衛星通信運用は含まない**
- security: PQC、機密計算、FHE、ZKP、AI モデル攻防、QKD、TEE、分散 ID 暗号。\
**通常の脆弱性公開・サイバー攻撃事件・ランサムは含まない**

[Vertical — application domain]

- bio: ゲノム編集、遺伝子治療、合成生物学、mRNA、AI 創薬手法、シーケンシング、\
Brain-Computer Interface、培養肉、新モダリティ臨床承認。\
**既存薬・製薬業績・動物学は含まない**
- energy: 核融合、SMR、第 4 世代炉、固体電池、フロー電池、水素、先進地熱、超伝導送電。\
既存発電（シリコン太陽光・陸上風力・火力・現行原子力）・電力会社業績は含まない
- space: 衛星、ロケット、宇宙探査、軌道インフラ、宇宙太陽光発電、衛星通信運用
- mobility: **人/物を運ぶ**自律機械 — 自動運転、新型 EV、トラック、ドローン物流、\
eVTOL、自律航行船。既存車種の販売・決算・配車アプリは含まない
- robotics: **運搬を主目的としない**自律物理機械 — ヒューマノイド、産業ロボ、\
サービスロボ、マニピュレータ、脚式。ロボット向けチップが主題なら semiconductor

鉄則: 既存 11 カテゴリに **明確に** フィットしない場合は、\
無理に押し込まず Step 0 に戻り out_of_scope を選ぶ。

# Step 2 — topic を決定する
category 内で「この記事は何の技術の話か？」を一言で表すラベルを決定する\
（LLM、量子コンピューティング、ヒューマノイドロボット、固体電池、自動運転 など）。\
応用先・製品名・会社名・動詞・イベント名は topic にしない。

【既存 topic の再利用（最優先）】
- 主題が既存と一致するなら必ず再利用する（name と label_ja の両方）
- **他カテゴリに同名・同義の既存 topic がある場合は category 選択を見直す**\
（例: "generative ai" が ai にあるなら computing で作らず ai を選ぶ）
- 迷ったら既存を選ぶ — 粒度を粗く保つのが目的

【新規作成ルール】
1. 業界で確立されたサブセクター名を使う\
（OK: "neuromorphic chip", "photonic computing" / NG: "battery manufacturing plant"）
2. 会社名・製品名を含めない（NG: "openai release", "tesla fsd"）
3. 動詞・イベント名・歴史語を含めない\
（NG: "launch", "acquisition", "takedown", "history"、語末も不可）
4. 既存の派生変種を作らない（"ai agents" があれば "ai agent debugging" は不可）
5. 応用先・対象領域を含めない
6. 形式: 小文字英語 / 最大 3 語（空白区切り、ハイフン不可）/ \
冠詞・前置詞（a / an / the / in / of）不可 / 語末は名詞

{existing_topics_section}
# Step 3 — reasoning
なぜこの category / topic を選んだのかを日本語で簡潔に記述。\
out_of_scope の場合は「なぜ 11 カテゴリのいずれにも該当しないか」を明示する。
"""


def _build_existing_topics_section(
    topics_by_category: dict[str, list[tuple[str, str]]] | None,
) -> str:
    """カテゴリ内の既存 Topic リスト（name, label_ja のペア）をプロンプトに挿入する。

    主題が同じなら必ず既存を再利用させるため、上限を設けず全件提示する。
    label_ja を併記することで、再利用時に AI が同じ日本語ラベルを返せる。
    """
    if not topics_by_category:
        return ""

    lines = [
        "Existing topics by category (name → label_ja). "
        "If the article's subject matches any of these, you MUST reuse both "
        "the name and the label_ja. Only create a new one when the subject "
        "is genuinely new.",
    ]
    for cat_slug, topics in topics_by_category.items():
        topic_list = ", ".join(f'"{name}" → "{label}"' for name, label in topics)
        lines.append(f"- {cat_slug}: [{topic_list}]")

    return "\n".join(lines) + "\n"


def _build_entities_section(entities: list[Entity]) -> str:
    """エンティティリストをプロンプト挿入用テキストに整形する。"""
    if not entities:
        return "(none)"
    return ", ".join(f"{e.name.root} ({e.type.root})" for e in entities)


def _to_domain(raw: ClassificationRawResponse) -> ClassificationResponse:
    """フラットな AI レスポンスをドメイン型 tagged union に詰め替える。

    category=OUT_OF_SCOPE のときは topic を捨て OutOfScope に、
    それ以外は全フィールドを Classified に移す。この関数が唯一の分岐点であり、
    以降のコードは union の ``match`` / ``isinstance`` で型安全に扱える。
    """
    if raw.category == ValidCategory.OUT_OF_SCOPE:
        return OutOfScope(reasoning=raw.reasoning)
    return Classified(
        category=raw.category,
        topic=raw.topic,
        topic_label_ja=raw.topic_label_ja,
        reasoning=raw.reasoning,
    )


class GeminiClassifier(BaseClassifier):
    """BaseClassifier の Gemini API 実装。"""

    MODEL = "gemini-2.5-flash-lite"
    RPM = 100
    RPD = 3000

    def __init__(self) -> None:
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise ConfigurationError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=api_key)

    async def classify(
        self,
        title_ja: str,
        summary_ja: str,
        entities: list[Entity],
        existing_topics_by_category: dict[str, list[tuple[str, str]]] | None = None,
    ) -> ClassificationResponse:
        """Stage 1 の出力を分類する。原文は読まない。"""
        entities_section = _build_entities_section(entities)
        existing_topics_section = _build_existing_topics_section(
            existing_topics_by_category,
        )

        prompt = CLASSIFICATION_PROMPT.format(
            title_ja=title_ja,
            summary_ja=summary_ja,
            entities_section=entities_section,
            existing_topics_section=existing_topics_section,
        )

        return await self._call_once(prompt)

    async def _call_api(self, prompt: str) -> ClassificationResponse:
        """Gemini の generate_content API を呼び出し構造化出力を受け取る。"""
        response = await self._client.aio.models.generate_content(
            model=self.MODEL,
            contents=prompt,
            config=GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=1024,
                response_mime_type="application/json",
                response_schema=ClassificationRawResponse,
            ),
        )
        parsed = response.parsed
        if not isinstance(parsed, ClassificationRawResponse):
            raise ProviderError(
                f"Gemini did not return ClassificationRawResponse "
                f"(got {type(parsed).__name__})"
            )
        return _to_domain(parsed)

    def _translate_error(self, exc: Exception) -> AnalysisDomainError:
        """Gemini SDK の例外を原因の所在で分類する。"""
        if isinstance(exc, ValidationError):
            return ProviderError(f"Invalid classification response schema: {exc}")

        if isinstance(exc, APIError):
            status = exc.status or ""
            message = exc.message or ""

            if "reported as leaked" in message:
                return ConfigurationError(f"API key leaked: {message}")

            if status in (
                "UNAUTHENTICATED",
                "PERMISSION_DENIED",
                "FAILED_PRECONDITION",
                "NOT_FOUND",
            ):
                return ConfigurationError(f"{status}: {message}")

            if status in ("INVALID_ARGUMENT", "DEADLINE_EXCEEDED"):
                return InvalidInputError(f"{status}: {message}")

            if status == "RESOURCE_EXHAUSTED":
                return RateLimitError(f"{status}: {message}")

            if isinstance(exc, ServerError):
                return ProviderError(f"{status}: {message}")

            return UnclassifiedError(
                f"Unhandled APIError {exc.code} {status}: {message}"
            )

        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return NetworkError(f"{type(exc).__name__}: {exc}")

        return UnclassifiedError(f"{type(exc).__name__}: {exc}")
