"""Gemini 実装の Classifier — Stage 2。"""

from __future__ import annotations

import structlog
from google import genai
from google.genai.errors import APIError, ServerError
from google.genai.types import GenerateContentConfig
from pydantic import ValidationError

from app.analysis.classifier.base import BaseClassifier
from app.analysis.classifier.schema import ClassificationResponse
from app.analysis.errors import (
    AnalysisDomainError,
    ConfigurationError,
    InvalidInputError,
    NetworkError,
    ProviderError,
    RateLimitError,
    UnclassifiedError,
)
from app.analysis.extraction.schema import EntityResponse
from app.config import settings

logger = structlog.get_logger(__name__)


CLASSIFICATION_PROMPT = """\
あなたは先端技術分野のテックニュース分類の専門家です。

タイトル: {title_ja}

サマリー:
{summary_ja}

エンティティ:
{entities_section}

Step 1 — category を決定する。
記事が最終的に何を生み出す領域か（主な artifact / output）で分類してください。\
使われている技術ではなく、出力される成果物の領域を見ます。\
例: 「AI が新材料を発見」は materials（成果物）であって ai（手段）ではありません。

以下から最も関連の強い category を 1 つだけ選択してください（slug は英語）:
- ai: AI モデル・サービス・エージェント、AI 業界の動向
  例: 新しい LLM のリリース、AI スタートアップの資金調達、AI 規制
  対象外: 他領域でツールとして使われた AI
- robotics: 自律ロボット、自動運転車、ドローン、eVTOL
  例: ヒューマノイドロボットのデモ、自動運転タクシー、ドローン配送
  境界: ロボット向けのチップが主題なら semiconductor
- semiconductor: チップ設計、製造、リソグラフィ、パッケージング
  例: 新プロセスノード、EUV 進展、チップレットパッケージング
  境界: 量子チップなら computing
- computing: 量子、ニューロモーフィック、光、DNA コンピューティング
  例: 量子誤り訂正、ニューロモーフィックチップ、光コンピューティング
- network: 6G、Open RAN、AI-RAN、SDN、海底ケーブル、データセンター間接続
  例: 6G 実証、Open RAN 導入、海底ケーブル敷設
- security: PQC、コンフィデンシャルコンピューティング、FHE、ZKP、AI セキュリティ
  例: 耐量子暗号標準、ゼロ知識証明システム
  境界: サイバー攻撃事例は新しい防御技術に関する場合のみ
- space: 衛星、ロケット、宇宙探査、軌道インフラ
  例: ロケット打ち上げ、衛星コンステレーション、火星探査
- bio: ゲノム編集、遺伝子治療、合成生物学、mRNA、AI 創薬
  例: CRISPR 治療承認、mRNA ワクチン、タンパク質構造予測
  境界: 「AI が新薬を発見」は bio（成果物が薬）
- materials: 新素材、3D プリンティング、ナノ加工
  例: 常温超伝導体、カーボンナノチューブのブレイクスルー、メタマテリアル
  境界: 「AI が新材料を発見」は materials
- energy: 核融合、SMR、次世代電池、水素、先端地熱
  例: 核融合マイルストーン、全固体電池、グリーン水素プラント

Step 2 — topic を決定する。
選んだ category 内で、簡潔な topic ラベルを割り当ててください。ルール:
- 小文字英語、2〜4 語、冠詞（a/an/the）不可
- category 内で確立された用語を使う
- 具体的に: 「semiconductor news」ではなく「euv lithography advancement」のように
{existing_topics_section}
Step 3 — impact_level を評価する（暫定）。
- low: 漸進的アップデート、マイナーな製品機能
- medium: 特定セクター内の注目すべき動向
- high: 業界の大きな変化、主要な製品ローンチ、大型資金調達
- critical: パラダイムを変えるブレイクスルー、重大な規制変更

Step 4 — reasoning を記述する。
なぜこの category / topic / impact_level を割り当てたのかを、\
日本語で簡潔に説明してください。
"""


def _build_existing_topics_section(
    topics_by_category: dict[str, list[str]] | None,
) -> str:
    """カテゴリ内の既存 Topic リスト（上位30件）をプロンプトに挿入する。"""
    if not topics_by_category:
        return ""

    lines = [
        "Existing topics by category (use these if applicable, "
        "create a new one only if none fit):"
    ]
    for cat_slug, topics in topics_by_category.items():
        topic_list = ", ".join(f'"{t}"' for t in topics[:30])
        lines.append(f"- {cat_slug}: [{topic_list}]")

    return "\n".join(lines) + "\n"


def _build_entities_section(entities: list[EntityResponse]) -> str:
    """エンティティリストをプロンプト挿入用テキストに整形する。"""
    if not entities:
        return "(none)"
    return ", ".join(f"{e.name.root} ({e.type.root})" for e in entities)


class GeminiClassifier(BaseClassifier):
    """BaseClassifier の Gemini API 実装。"""

    MODEL = "gemini-2.5-flash-lite"
    RPM = 50
    RPD = 1500

    def __init__(self) -> None:
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise ConfigurationError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=api_key)

    async def classify(
        self,
        title_ja: str,
        summary_ja: str,
        entities: list[EntityResponse],
        existing_topics_by_category: dict[str, list[str]] | None = None,
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
                response_schema=ClassificationResponse,
            ),
        )
        parsed = response.parsed
        if not isinstance(parsed, ClassificationResponse):
            raise ProviderError(
                f"Gemini did not return ClassificationResponse "
                f"(got {type(parsed).__name__})"
            )
        return parsed

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
