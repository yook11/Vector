# Article Analyses テーブル設計（セキュア・バイ・デザイン）

> 作成日: 2026-03-22
> ソース: `specs/db-domain-model.md` セクション 2.4 ArticleAnalysis
> ギャップ分析: GAP-3（1:1 制約、ai_models テーブル削除）、GAP-4（Translation Table 廃止、sentiment 削除、reasoning 追加）

## 1. 概要

個別記事に対する LLM の翻訳・要約・分析結果。「参考情報」であり事実の断定ではないという性質を構造的に持つ。
NewsArticle と 1:1 の関係。分析結果は作成後に変更されない不変のエンティティ。

### 現行 → 新設計の変更点

| 項目 | 現行 | 新設計 |
|------|------|--------|
| テーブル名 | `analyses` | `article_analyses` |
| UNIQUE制約 | `(news_article_id, ai_model_id)` | `news_article_id` 単独（1:1 保証） |
| `ai_model_id` | FK → `ai_models` テーブル | `ai_model` VARCHAR(100)（文字列カラム） |
| `sentiment` | positive/negative/neutral | 削除（外部データなしでは有効でない） |
| `impact_score` | INT 1-10 | 削除（旧 `impact_level` enum 4 段階を経て、2026-04 に完全廃止） |
| 翻訳 | `analysis_translations`（別テーブル） | `translated_title` として統合。LLM が分析時に一緒に生成 |
| `translated_content` | — | 持たない。全文翻訳は需要が顕在化した時点で機械翻訳 API で対応 |
| `embedding` | `news_articles` に格納 | ここに移動（AI 処理の産物） |
| `embedding_model` | なし | 追加（embedding モデルの追跡） |

### 削除するテーブル

| テーブル | 理由 |
|------|------|
| `ai_models` | 過剰な正規化。モデルは1種類しか使わないため文字列カラムで十分（GAP-3） |
| `analysis_translations` | Translation Table 廃止。多言語対応しないため翻訳テーブル不要（GAP-4） |
| `analysis_investment_categories` | `investment_categories` 削除に伴い不要（GAP-1） |

### 翻訳パイプラインの統合について

当初 translated_title は機械翻訳 API（Google Cloud Translation / DeepL 等）で別パイプラインとして生成する設計だったが、以下の理由で LLM 分析に統合した。

- 機械翻訳を別パイプラインにすると、生成元（LLM vs 翻訳API）・完了タイミング・モデル追跡が混在し、テーブル設計が不自然になる
- LLM は分析のために既に記事を読んでいるため、タイトル翻訳の追加コストは軽微
- テーブル内の全属性が同一の `ai_model` と `analyzed_at` に対応し、一貫性が保たれる

全文翻訳（translated_content）は LLM で行うとリソース過剰であり、要約 + reasoning で十分な情報をユーザーに提供できるため持たない。需要が顕在化した時点で機械翻訳 API で対応する。

## 2. 属性の不変条件

### id

| 項目 | 定義 |
|------|------|
| 型 | Integer (AUTO INCREMENT) |
| DB制約 | PRIMARY KEY |
| 不変条件 | 自動採番、変更不可 |

### news_article_id

| 項目 | 定義 |
|------|------|
| 型 | Integer |
| DB制約 | `NOT NULL`, `UNIQUE`, `FOREIGN KEY REFERENCES news_articles(id) ON DELETE CASCADE` |
| 不変条件 | 分析対象の記事。1記事につき1分析（UNIQUE で 1:1 保証）。変更不可 |
| 備考 | CASCADE の理由: 記事が削除されたら分析結果も存在意義を失う。記事の削除自体が稀なケース（誤取り込みの除去、法的理由等）であり、その際に孤立レコードを残さない |

### translated_title

| 項目 | 定義 |
|------|------|
| 型 | VARCHAR(500) |
| DB制約 | `NOT NULL` |
| 不変条件 | LLM が分析時に生成した日本語タイトル。変更不可 |
| 備考 | 分析パイプラインに統合されているため、分析完了時には必ず存在する。500文字は original_title と同じ上限 |

### summary

| 項目 | 定義 |
|------|------|
| 型 | TEXT |
| DB制約 | `NOT NULL` |
| 不変条件 | LLM が生成した記事の要約。変更不可 |
| 備考 | 長さは LLM のプロンプトで制御する。DB 側で長さ制限はかけない |

### ~~impact_level~~ — 2026-04 廃止

旧設計では `VARCHAR(20)` + `CHECK (impact_level IN ('low','medium','high','critical'))` の業界インパクト度カラムを保持していたが、LLM の判定が実用上有効でないため代替指標を立てずに撤去した。Analysis アグリゲートには現在「優先度」概念が存在しない。

### reasoning

| 項目 | 定義 |
|------|------|
| 型 | TEXT |
| DB制約 | `NOT NULL` |
| 不変条件 | LLM が生成したインパクト判定の根拠説明。自然言語。変更不可 |
| 備考 | ユーザーはこの説明を読んで自分で投資判断の材料にする。固定ラベル（sentiment 等）ではなく自然言語で表現することで、AI の主観性の問題を回避。長さは LLM のプロンプトで制御する |

### ai_model

| 項目 | 定義 |
|------|------|
| 型 | VARCHAR(100) |
| DB制約 | `NOT NULL` |
| 不変条件 | 分析に使用した LLM モデル名（例: "gemini-2.0-flash"）。監査属性。変更不可 |
| 備考 | `ai_models` テーブル（正規化）を廃止し文字列カラムに変更（GAP-3）。モデルは1種類しか使わないため正規化は過剰。分析時点のモデルを記録することで、結果の追跡可能性を確保 |

### analyzed_at

| 項目 | 定義 |
|------|------|
| 型 | TIMESTAMP WITH TIME ZONE |
| DB制約 | `NOT NULL`, `DEFAULT NOW()` |
| 不変条件 | 分析実行日時。変更不可 |
| 備考 | `created_at` ではなく `analyzed_at` とする理由: このテーブルは分析結果であり、「いつ分析したか」がドメイン的に意味を持つ |

### embedding

| 項目 | 定義 |
|------|------|
| 型 | VECTOR(768) |
| DB制約 | NULLABLE |
| 不変条件 | 記事内容のベクトル表現。変更不可 |
| 備考 | 分析パイプラインとは別のタイミングで生成される可能性があるため NULLABLE。将来 NewsEvent のクラスタリング基盤として使用。`news_articles` テーブルから移動した理由: embedding は AI が記事内容から生成した数値表現であり、記事そのものの事実ではなく分析の産物 |

### embedding_model

| 項目 | 定義 |
|------|------|
| 型 | VARCHAR(100) |
| DB制約 | NULLABLE |
| 不変条件 | embedding 生成に使用したモデル名（例: "text-embedding-004"）。監査属性。変更不可 |
| 備考 | embedding が NULL のとき embedding_model も NULL。両方セットで存在する。embedding モデルが変わるとベクトル空間が変わり、異なるモデルの embedding 同士は比較できないため、追跡が必要 |

## 3. エンティティレベルの不変条件

| 制約 | 実現レイヤー | 説明 |
|------|-------------|------|
| 分析結果の不変性 | 設計原則 | 作成後に変更されない。新しいモデルは新しい記事の分析に適用する。再分析は通常運用ではない |
| 1記事1分析 | DB層（UNIQUE） | news_article_id の UNIQUE 制約で 1:1 を保証 |
| embedding と embedding_model の整合性 | アプリ層 | 両方 NULL または両方 非NULL。片方だけ存在する状態は不正 |
| LLM 出力のバリデーション | アプリ層 | category / topic が想定範囲か、出力が期待フォーマットかを検証 |
| プロンプトインジェクション対策 | 多層 | プロンプト設計（デリミタ、システム指示）+ アプリ層バリデーション + DB CHECK 制約 |

## 4. 多層防御サマリ

| レイヤー | 防御内容 |
|----------|---------|
| **プロンプト層** | 記事内容を明確なデリミタで区切り、コンテンツ内の指示に従わないようシステムプロンプトで指定 |
| **アプリ層** | LLM 出力のフォーマット検証、category / topic VO バリデーション、保存時のサニタイズ |
| **DB層** | UNIQUE（news_article_id で 1:1 保証）、NOT NULL、FK CASCADE |

## 5. 設計判断の記録

| 判断 | 結論 | 理由 |
|------|------|------|
| 翻訳パイプラインの統合 | LLM 分析時にタイトル翻訳も実行 | 別パイプラインにすると生成元・タイミングが混在しテーブル設計が不自然になる。LLM の追加コストは軽微 |
| translated_content の除外 | 持たない | 全文翻訳は LLM ではリソース過剰。要約 + reasoning で十分な情報を提供。需要が顕在化した時点で機械翻訳 API で対応 |
| 不変性 | 作成後変更不可 | 新モデルは新記事に適用する原則。再分析が必要な場合はデータマイグレーションとして対応（通常運用ではない） |
| impact_score → impact_level → 廃止 | 数値1-10 を enum 4段階に変更し、その後 2026-04 に完全廃止 | enum でも LLM 判定が安定しなかったため、代替指標を立てずに撤去 |
| sentiment の削除 | 持たない | AI 単独の主観ラベルであり、外部データ（SNS センチメント等）の裏付けがない段階では有効でない。将来民意を反映できる段階で再追加を検討 |
| embedding の移動 | news_articles → article_analyses | embedding は AI が記事内容から生成したベクトル表現。記事の事実ではなく分析の産物。翻訳・要約と同じ性質 |
| embedding_model の追加 | レコードごとに記録 | embedding モデルが変わるとベクトル空間が変わり比較不可能になる。どのレコードがどのモデルで生成されたか追跡が必要 |
| analyzed_at の命名 | created_at ではなく analyzed_at | 分析結果テーブルとして「いつ分析したか」がドメイン的に意味を持つ |
