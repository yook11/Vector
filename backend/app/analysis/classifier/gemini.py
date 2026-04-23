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
  例: 耐量子暗号標準、ゼロ知識証明システム、AI モデルへの攻撃と防御
  境界: サイバー攻撃事例（脆弱性報告、データ漏洩、ランサムウェア等）は\
新しい防御技術が主題の場合に限る。それ以外は out_of_scope
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
- out_of_scope: 上記 10 カテゴリのいずれにも該当しない
  例: 先端技術と無関係な記事\
（ビジネス一般、エンタメ、スポーツ、政治、ゴシップ等）、\
IT 業界の話題であっても先端要素を含まないもの\
（一般的な SaaS リリース、通常の会社情報等）、\
境界事例で既存の 10 カテゴリに分類できないもの

Step 2 — topic を決定する。
選んだ category 内で、記事の「主題（subject）」を表す topic ラベルを割り当ててください。

【トピック選択の優先順位】
1. 既存トピックの中に主題が同じものがあれば、必ずそれを再利用する
2. 既存に該当が無い場合のみ、新規トピックを作る
3. 迷ったら既存トピックを選ぶ

【新規トピックを作る場合のルール】
1. 業界で確立されたサブセクター名を使う
   例: 「neuromorphic chip」OK、「nvidia chip launch」NG
2. 会社名・製品名を含めない
   例: 「openai release」NG、「llm」OK
3. 動詞・イベント名を含めない
   例: 「launch」「acquisition」「debugging」「development」を語末に付けない
4. 既存トピックの派生バリエーションを作らない
   例: 「ai agents」が既に存在するときに「ai agent debugging」を作らない
5. 命名形式: 小文字英語、2〜4 語、冠詞（a/an/the）不可

【out_of_scope の場合】
記事内容を端的に表す自然な topic（例: "celebrity gossip", "generic saas release"）\
で構いません。同じガイドラインを満たさなくてもよい。
{existing_topics_section}
Step 2.5 — topic_label_ja を決定する。
topic に対応する日本語の表示ラベル（最大 20 文字）を出力してください。
- 既存トピック再利用時: 上で提示された label_ja をそのまま返す
- 新規生成時: 業界で一般的な日本語表記を使う（例: "llm" → "大規模言語モデル"）
- 全角英数字は使わない（"ＡＩ" NG、"AI" OK）
- 動詞・冠詞を含めず、subject 中心の名詞句にする

Step 3 — impact_level を評価する。
- low: 漸進的アップデート、マイナーな製品機能、あるいは対象外記事
- medium: 特定セクター内の注目すべき動向
- high: 業界の大きな変化、主要な製品ローンチ、大型資金調達
- critical: パラダイムを変えるブレイクスルー、重大な規制変更

Step 4 — reasoning を記述する。
なぜこの category / topic / impact_level を割り当てたのかを、\
日本語で簡潔に説明してください。out_of_scope を選んだ場合は「なぜ既存 10 カテゴリに\
該当しないか」を明示してください。
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


def _build_entities_section(entities: list[EntityResponse]) -> str:
    """エンティティリストをプロンプト挿入用テキストに整形する。"""
    if not entities:
        return "(none)"
    return ", ".join(f"{e.name.root} ({e.type.root})" for e in entities)


def _to_domain(raw: ClassificationRawResponse) -> ClassificationResponse:
    """フラットな AI レスポンスをドメイン型 tagged union に詰め替える。

    category=OUT_OF_SCOPE のときは topic / impact_level を捨て OutOfScope に、
    それ以外は全フィールドを Classified に移す。この関数が唯一の分岐点であり、
    以降のコードは union の ``match`` / ``isinstance`` で型安全に扱える。
    """
    if raw.category == ValidCategory.OUT_OF_SCOPE:
        return OutOfScope(reasoning=raw.reasoning)
    return Classified(
        category=raw.category,
        topic=raw.topic,
        topic_label_ja=raw.topic_label_ja,
        impact_level=raw.impact_level,
        reasoning=raw.reasoning,
    )


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
