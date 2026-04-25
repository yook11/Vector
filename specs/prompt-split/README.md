# Prompt Split — 分析プロンプト2段階分離

> ステータス: 設計確定（実装待ち）

## 背景

現行の分析パイプラインは 1 プロンプトで 5 つのタスク（翻訳・要約・カテゴリ分類・トピック抽出・インパクト評価）を同時実行している。
これにより以下の問題が発生している。

### 問題1: 1プロンプトに異なる性質のタスクが混在

抽出（翻訳・要約）と判断（分類・評価）が同居し、各タスクの精度が単独実行時より低下する。
特にカテゴリ分類のような境界判定は、翻訳・要約のコンテキストに引っ張られてブレやすい。

### 問題2: プロンプトに改善余地がない

原文（最大 8000 文字）+ 既存トピックリスト + 5 タスク分の指示でプロンプトが詰まっている。
カテゴリ定義の詳細化、境界ケースの例示、Few-shot 例の追加など、
精度向上に必要な投資を行う空間がない。

### 問題3: エンティティ抽出が存在しない

記事中の企業名・技術名・製品名が構造化されておらず、
Trend Detection（軸2）に進むためのデータ基盤がない。
現行プロンプトにエンティティ抽出を追加すると 6 つ目のタスクが加わり問題 1 が悪化する。

### 問題4: 失敗時の再実行コストが大きい

翻訳・要約が正しくてもカテゴリ判定だけ不正値を返した場合、全タスクをリトライする。
RPD=1500 の制約下で無駄が大きい。

### 問題5: カテゴリ体系の変更が全記事再分析を要求する

カテゴリ定義を変更したとき、翻訳・要約（記事固有の事実であり体系と無関係）まで
含めて全記事を再実行する必要がある。

### 問題6: 要約に判断が混入している

現行の 3 行要約フォーマット（事実 / 業界影響 / 投資示唆）は、
Line 2・3 で LLM に判断・推測を要求している。
要約本来の「事実を正確に伝える」品質を希釈している。

## 設計方針

### LLM の役割: 事実の構造化

LLM に求めるのは **事実の抽出と構造化** のみ。
影響度評価・投資判断は将来的にデータ駆動（株価相関・過去パターン分析等）で行う。

> **記録**: 旧 `impact_level`（業界インパクト度 4 段階 enum）は本ドキュメント当時「LLM の暫定値」として残されていたが、2026-04 に完全廃止された。本ドキュメント以下の `impact_level` 言及は歴史的経緯として残している。

### 分割の軸: 原文が要るか要らないか

| | Stage 1 | Stage 2 |
|---|---|---|
| 入力 | 原文（title, description, content） | Stage 1 の出力 |
| 性質 | 抽出 | 判断 |
| ライフサイクル | 記事に固定 | 体系変更で再実行可能 |

この分割はタスクの認知モードとライフサイクルの両方で一致する。
原文に依存する処理は記事固有の事実抽出であり、
原文に依存しない処理は体系に基づく判断である。

## Stage 1 — Content Extraction

原文を読み、情報を取り出す。判断はしない。

### 入力

- `title`: 英語の記事タイトル
- `description`: 英語の記事概要（nullable）
- `content`: 英語の記事本文（最大 8000 文字、nullable）

### 出力

```json
{
  "title_ja": "日本語タイトル",
  "summary_ja": "事実ベースの日本語要約",
  "entities": [
    {"name": "Anthropic", "type": "company"},
    {"name": "Claude 4", "type": "product"},
    {"name": "constitutional AI", "type": "technology"}
  ]
}
```

### title_ja

記事タイトルの正確な日本語翻訳。

### summary_ja

記事に書かれた事実を日本語で正確に再構成する。
要約者による判断・評価・推測を加えない。

含めるべき情報:
- **主体と行為**: 誰が、何を、どこで行ったか
- **具体的な数値**: 金額、規模、日付、バージョン番号、性能指標
- **技術的な新規性**: 何が新しいのか、既存手法との違い

含めないこと:
- 業界への影響の評価
- 投資示唆・市場予測
- 記事に存在しない推測

### entities

記事中に登場する固有名詞の構造化リスト。

| type | 対象 | 例 |
|---|---|---|
| `company` | 企業・組織 | Anthropic, TSMC, NASA |
| `product` | 製品・サービス名 | Claude 4, GPT-5, Falcon 9 |
| `technology` | 技術・フレームワーク | constitutional AI, EUV lithography, CRISPR |

- 記事中に明示的に登場するもののみ抽出する
- 一般名詞（"AI", "semiconductor" 等）は含めない
- 同一エンティティの重複は除去する

## Stage 2 — Classification

Stage 1 の構造化出力に対して判断を下す。原文は読まない。

### 入力

- Stage 1 の出力（`title_ja`, `summary_ja`, `entities`）
- カテゴリ定義（10 カテゴリ + 説明・境界ケース・除外条件）
- 既存トピックリスト（カテゴリ内上位 30 件）

### 出力

```json
{
  "category": "ai",
  "topic": "constitutional ai advancement",
  "reasoning": "..."
}
```

### 分割による Stage 2 の改善点

原文のトークンを消費しないため、以下をプロンプトに含める余地が生まれる:
- カテゴリ定義の詳細化（現行は各 1 行のみ）
- 境界ケースの例示（例: 「AI が新素材を発見」→ materials）
- 除外条件の明示
- Few-shot 例

## データモデル

### ArticleAnalysis（変更）

Stage 1 と Stage 2 の結果を1つのテーブルに保持する。
2段階で生成するのはパイプラインの都合であり、ドメインから見れば「記事の分析結果」は1つの概念。

| フィールド | 型 | 現行 | 変更後 | 理由 |
|---|---|---|---|---|
| `translated_title` | String(500) | NOT EMPTY | NOT EMPTY | Stage 1 で必ず書く |
| `summary` | Text | NOT EMPTY | NOT EMPTY | Stage 1 で必ず書く |
| `topic_id` | FK | NOT NULL | **nullable** | 未分類状態が存在する |
| `reasoning` | Text | NOT EMPTY | **nullable** | 未分類状態が存在する |

`topic_id IS NULL` = 抽出済み・未分類。これは抽出と分類が別の関心事であるというドメインの反映。

### ArticleEntity（新規）

```
ArticleEntity
  id: int                          PK
  article_analysis_id: int         FK → article_analyses.id
  name: str                        抽出された名前
  type: EntityType (StrEnum)       company / product / technology
```

- マスターテーブルは作らない。LLM の出力をそのまま保存する
- 正規化（表記ゆれ統合）は将来必要になったら `normalized_name` カラムを追加する
- トレンド集計は `GROUP BY name, type` で動作する

### 公開クエリでの中間状態の除外

Stage 2 未完了の記事（`topic_id IS NULL`）は UI の公開クエリでは除外する。
分類が完了した記事のみユーザーに表示される。

```sql
-- 公開クエリには必ずこの条件を含める
WHERE article_analyses.topic_id IS NOT NULL
```

## アプリケーション側の責務

LLM にやらせないこと:

| 処理 | 方法 |
|---|---|
| エンティティ正規化 | 表記ゆれ統合（"NVIDIA" / "Nvidia Corp" → 統一）— 文字列処理 |
| スコープ判定 | カテゴリ結果から導出（カテゴリが付けば対象内） |
| トレンド検出 | エンティティの時間窓ごと頻度集計 — SQL/Redis |
| トピック名正規化 | 既存の `normalize_topic_name()` |
| バリデーション | 既存の `VALID_CATEGORIES` チェック等 |

## パイプライン構造

### 現行

```
fetch_metadata → fetch_content → analyze_article → generate_embedding
```

### 変更後

```
fetch_metadata → fetch_content → extract_content → classify_content → generate_embedding
```

### 永続化タイミング

各ステージが自分の結果を永続化する。Stage 1 の API コールを無駄にしない。

```
extract_content → Stage 1 LLM
  → ArticleAnalysis 作成 (translated_title, summary, ai_model)
  → ArticleEntity 書き込み
  → classify_content.kiq(article_id)

classify_content → DB から Stage 1 結果を読み出し (translated_title, summary, entities)
  → Stage 2 LLM
  → ArticleAnalysis 更新 (topic_id, reasoning)
  → generate_embedding.kiq(article_id)
```

- Stage 2 が失敗しても Stage 1 の結果は DB に残る
- リトライは `classify_content` だけで済む
- カテゴリ体系変更時は `classify_content` を全記事に再実行するだけ

## 現行プロンプトからの変更点

| 部分 | 現行 | 変更後 |
|---|---|---|
| プロンプト数 | 1 | 2（Stage 1 + Stage 2） |
| summary_ja | 3 行固定（事実/影響/投資） | 事実ベースの自由形式 |
| entities | なし | `[{name, type}]` 構造化リスト（3型） |
| Stage 2 の入力 | 原文 | Stage 1 の出力のみ（DB から読み出し） |
| カテゴリ定義の詳細度 | 各 1 行 | 詳細化可能（原文トークン不要） |
| 中間状態 | 存在しない | 抽出済み・未分類（`topic_id IS NULL`） |

## Trend Detection（軸2）との接続

Stage 1 の `entities` 出力がそのまま軸 2 のデータ源になる。

```
Stage 1 が記事ごとにエンティティを抽出
  → ArticleEntity として DB に保存
  → アプリケーション側でエンティティ正規化
  → 集計ジョブが時間窓ごとに頻度をカウント
  → 急増 = トレンド
```

LLM の仕事はエンティティ抽出まで。トレンド検出自体は統計処理。

## 関連

- [topic-tagging/ai-pipeline.md](../topic-tagging/ai-pipeline.md) — 現行プロンプト設計（本スペックで置換）
- [trend-detection/](../trend-detection/) — 軸 2 構想（本スペックの entities が基盤）
