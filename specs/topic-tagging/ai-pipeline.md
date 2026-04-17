# Topic Tagging — AI パイプライン設計

## 概要

現行の Keyword 選択（閉じたリストから最大3個）を廃止し、
Category 判定 + Topic 自由生成に置き換える。

## プロンプト設計

### 設計原則

1. **Category → Topic の順序**: Category を先に確定させることで Topic 生成の文脈が絞られる。
   Gemini は propertyOrdering に従って生成するため、構造的に CoT が発生する
2. **既存 Topic リストの提示**: カテゴリ内の既存 Topic 上位30件を渡し、
   「該当すれば使え、なければ新規作成可」と指示する。語彙の収束と自由生成を両立
3. **選択の強制ではない**: 現行 Keyword の失敗は「リスト外を選べない」制約にあった。
   新設計ではリストはガイドであって制約ではない

### ベースプロンプト

```
You are an expert tech news analyst specializing in emerging technologies \
with a focus on investment implications.

Analyze the following English tech news article and respond ONLY with \
a valid JSON object. Do not include markdown code fences or any text \
outside the JSON.

Article title: {title}
Article description: {description}
{content_section}

Classify this article following these steps:

Step 1 — Determine the category.
Select the single most relevant category from:
- ai_ml: Artificial intelligence and machine learning
- biotech: Biotechnology, pharmaceuticals, genomics
- energy: Energy generation, storage, and sustainability
- fintech: Financial technology, digital payments, blockchain
- materials: Materials science, advanced materials, nanomaterials
- quantum: Quantum computing, quantum sensing, quantum networking
- robotics: Robotics, autonomous vehicles, industrial automation
- semiconductor: Chip design, manufacturing, lithography, and policy
- space: Space launch, satellites, lunar exploration
- telecom: Telecommunications, 5G/6G, network infrastructure

Step 2 — Determine the topic.
Given the category, assign a concise topic label that captures what \
this article is specifically about. Rules:
- Lowercase English, 2-4 words, no articles (a/an/the)
- Use established terminology within the category
- Be specific: prefer "euv lithography advancement" over "semiconductor news"
{existing_topics_section}

Return a JSON object with fields in this exact order:
{{
  "category": "one of the category slugs above",
  "topic": "concise topic label, 2-4 words, lowercase English",
  "title_ja": "Japanese translation of the article title (accurate, concise)",
  "summary_ja": "3-line summary in Japanese. Line 1: key facts. \
Line 2: industry impact. Line 3: investment implications. \
Separate lines with \\n",
  "impact_level": "one of: low, medium, high, critical — how much this \
news affects the market",
  "reasoning": "Brief explanation in Japanese of why you assigned \
this impact level"
}}

Rules:
- All Japanese text must be natural, professional Japanese
- impact_level MUST be exactly one of: "low", "medium", "high", "critical"
- If description is empty, analyze based on the title alone
- When full article content is provided, use it for deeper analysis
```

### 既存 Topic セクションの動的生成

カテゴリ内の既存 Topic を記事数降順で上位30件取得し、プロンプトに挿入する。

```python
def _build_existing_topics_section(
    topics_by_category: dict[str, list[str]] | None,
) -> str:
    """カテゴリ内の既存 Topic リスト（上位30件）をプロンプトに挿入する。"""
    if not topics_by_category:
        return ""

    lines = ["Existing topics by category (use these if applicable, "
             "create a new one only if none fit):"]
    for cat_slug, topics in topics_by_category.items():
        topic_list = ", ".join(f'"{t}"' for t in topics[:30])
        lines.append(f"- {cat_slug}: [{topic_list}]")

    return "\n".join(lines) + "\n"
```

### 既存 Topic の取得クエリ

```python
stmt = (
    select(Category.slug, Topic.name)
    .join(Topic, Topic.category_id == Category.id)
    .join(
        ArticleAnalysis,
        ArticleAnalysis.topic_id == Topic.id,
    )
    .group_by(Category.slug, Topic.id, Topic.name)
    .order_by(Category.slug, func.count().desc())
)
```

各カテゴリの上位30件を dict に詰めて `_build_existing_topics_section()` に渡す。
Topic が蓄積されていない初期段階では空文字列が返り、プロンプトに影響しない。

## 現行プロンプトとの差分

| 部分 | 現行 | 新 |
|---|---|---|
| JSON フィールド順 | title_ja → summary_ja → impact_level → reasoning | **category → topic →** title_ja → summary_ja → impact_level → reasoning |
| Category | なし (keyword 経由で間接的) | **slug リスト + 説明から1つ選択** |
| Topic | なし | **2-4語の英語小文字ラベルを自由生成** |
| 既存 Topic リスト | なし | **カテゴリ内上位30件を提示 (該当すれば使え、なければ新規作成可)** |
| Keyword 候補 | 全72語を category 別に渡す | **廃止** |
| Keyword 選択指示 | "select up to 3 from candidates" | **廃止** |

## パース処理

### レスポンス形式

```json
{
  "category": "biotech",
  "topic": "ai drug discovery",
  "title_ja": "...",
  "summary_ja": "...",
  "impact_level": "high",
  "reasoning": "..."
}
```

### バリデーション

1. `category`: 10個の slug のいずれかであること。不一致ならエラー
2. `topic`: 文字列であること。TopicName VO でバリデーション
3. 既存フィールド (`title_ja`, `summary_ja`, `impact_level`, `reasoning`): 現行と同じ

### Topic の正規化 (コード側)

```python
def normalize_topic_name(raw: str) -> str:
    """AI 出力の topic ラベルを正規化する。"""
    name = raw.strip().lower()
    name = re.sub(r"[\s-]+", " ", name)  # 連続空白・ハイフンをスペースに統一
    return name
```

- 小文字化
- 連続空白・ハイフンをスペース1個に統一
- 前後の空白を除去

## 永続化フロー (find-or-create)

```
AI レスポンス
  → category slug から Category.id を取得
  → topic 名を正規化
  → (name, category_id) で Topic を検索
    → 存在すれば → その topic_id を使用
    → 存在しなければ → Topic レコードを INSERT
  → article_analyses.topic_id に設定
```

### 廃止される処理

- `keywords_by_category` dict の構築 (全 Keyword をカテゴリ別にグループ化)
- Keyword 候補リストのプロンプト追記
- AI レスポンスの keyword フィルタリング (`all_candidates` との照合)
- `ArticleKeyword` レコードの作成
