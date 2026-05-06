# ドメイン棚卸し

## アプリケーション概要

先端テクノロジーのニュースをAIで分析・翻訳し、投資家向けに提供するアプリケーション。

### 解決する課題

- 先端テックのニュースは英語記事が多く、日本語話者の投資家が情報を得るまでにタイムラグがある
- ニュースに登場する企業の株価・関連ニュースなど、複数ソースを横断しないと得られない情報がある
- それらを一つの場所でスピード感を持って確認できるようにする

### 主要機能

- [ ] テックニュースの収集・AI分析・日本語翻訳
- [ ] ニュースの閲覧・検索
- [ ] （未実装）ニュースに登場する企業情報へのアクセス
- [ ] （未実装）企業の株価表示
- [ ] （未実装）企業名による関連ニュース横断検索

---

## ドメイン概念の棚卸し

> **使い方**: 各概念について、ClaudeCodeと壁打ちしながら「制約」「問題点」を埋めていく。
> 第2章の深いモデリングのように「この値がゼロだったら？」「マイナスだったら？」「空文字だったら？」と問いかけて、暗黙のルールを明示化する。

---

### ニュース記事（Article）

| 項目 | 内容 |
|------|------|
| 概念の説明 | 収集・翻訳されたテックニュース1件分のデータ |
| 現在の実装 | `backend/app/models/news.py` → `NewsArticle(SQLModel)` |

#### 属性の棚卸し

| 属性名 | 現在の型 | ドメイン上の制約 | 問題点・メモ |
|--------|----------|-----------------|-------------|
| タイトル（title_original） | `str(max_length=500)` NOT NULL | 空文字は防がれていない | news_fetcher.py で `strip_html_tags(entry.get("title", ""))[:500]` — 空文字フォールバックあり。NOT NULLだが `""` は通る |
| 説明文（description_original） | `str \| None` | nullable。長さ制限なし | RSS の summary/description をサニタイズして格納。Text型相当で上限なし |
| 本文（content） | `str \| None` | nullable | `config.content_max_length=8000` でフェッチ時に切り詰め。DB上は長さ制限なし |
| コンテンツ抽出日時（content_fetched_at） | `datetime(tz=True) \| None` | nullable | Noneなら未抽出。抽出済みかの判定に使用 |
| コンテンツ抽出試行回数（content_fetch_attempts） | `int` default=0 | NOT NULL | `content_max_fetch_attempts`(=3) 以上でスキップ — 暗黙のサーキットブレーカー |
| ソースURL（url） | `str(max_length=2048)` UNIQUE, NOT NULL | `is_safe_url()` で http/https のみ許可 | 同一性判断の主キー的役割。XSS対策あり |
| GUID（guid） | `str(max_length=2048) \| None` UNIQUE | nullable | RSSの `<guid>` タグ、なければ `<link>` にフォールバック。重複検出の第一キー |
| 公開日時（published_at） | `datetime(tz=True) \| None` | nullable | **未来日時の制約なし**。RSSパース失敗時にNone。dedup の時間窓（±3日）に影響 |
| 収集日時（fetched_at） | `datetime(tz=True)` NOT NULL | default=now(UTC) | 常にシステム時刻。ユーザー入力ではない |
| ソース名（source） | `str(max_length=100)` NOT NULL | NewsSource.name からコピー | **source_id と二重管理**。整合性は保証されない |
| ソースID（source_id） | `int \| None` FK → news_sources | SET NULL on delete | NewsSource削除時に孤立記事が残る |
| 埋め込みベクトル（embedding） | `Vector(768) \| None` | nullable | 768次元固定（Gemini API依存）。空ベクトル（全ゼロ）の検証なし |
| 重複グループID（article_group_id） | `int \| None` FK → article_groups | SET NULL on delete | 重複グループ未検出の記事はNone |
| AI分析結果 | リレーション → AnalysisResult[] | 0件 = 未分析 | 分析未完了を示す明示的なステータスフィールドはない |
| キーワード | 中間テーブル → NewsKeyword[] | 0件以上 | AI分析時にAIが選択してタグ付け。自由入力ではなく固定キーワードから選択 |

#### 深掘りすべき問い

- **記事の「同一性」は何で判断する？**
  → guid (UNIQUE) → url (UNIQUE) の二段階。guid はRSSの `<guid>` タグ、なければ `<link>` にフォールバック
- **同じニュースが複数ソースから収集された場合、どう扱う？**
  → guid/url で完全一致を排除。さらに embedding のコサイン距離（閾値 `dedup_similarity_threshold=0.15`）で意味的重複を検出し、ArticleGroup でグループ化。代表記事（canonical）を選出して表示
- **翻訳の品質が低い場合、公開すべきか？**
  → 品質フラグや非公開状態は存在しない。AI出力をそのまま公開する設計。品質管理の仕組みは未実装
- **記事のライフサイクルは？**
  → 収集(fetched_at) → コンテンツ抽出(content_fetched_at) → AI分析(AnalysisResult.analyzed_at) → 埋め込み生成(embedding != None) → 重複検出(article_group_id != None)。ただし**このライフサイクルはコード上で暗黙的**（状態フィールドや enum は存在しない）

#### 暗黙のルール

- `title_original` が空文字 `""` でも DB に保存可能（NOT NULL だが空文字は NULL ではない）
- `content_fetch_attempts >= 3` の記事はコンテンツ抽出がスキップされる — 暗黙のサーキットブレーカー
- `source` フィールドと `source_id` の二重管理。source_id は FK だが source は単なる文字列コピー。ソース名が変更されても既存記事の source は追従しない
- AI分析の入力に `content`（全文）が含まれるかは記事の抽出状態次第 — 全文なしの分析と全文ありの分析が品質的に異なるが、区別されない

---

### ニュースソース（NewsSource）

| 項目 | 内容 |
|------|------|
| 概念の説明 | ニュースの収集元となるメディア・サイト |
| 現在の実装 | `backend/app/models/news_source.py` → `NewsSource(SQLModel)` |

#### 属性の棚卸し

| 属性名 | 現在の型 | ドメイン上の制約 | 問題点・メモ |
|--------|----------|-----------------|-------------|
| ソース名（name） | `str(max_length=200)` NOT NULL | スキーマ側でホワイトリスト検証（`[\w \-\.]+`） | **モデル側には検証なし**。直接DB操作で不正値が入りうる |
| ソース種別（source_type） | `SourceType(StrEnum)` NOT NULL | "rss" \| "api" の2値 | DB側はCHECK制約なし（enum検証はPython層のみ） |
| サイトURL（site_url） | `str(max_length=2048) \| None` | nullable。スキーマ側でURLスキーム検証 | 表示用。収集には使われない |
| フィードURL（feed_url） | `str(max_length=2048) \| None` UNIQUE | nullable | **RSSソースでは必須だがDB制約では強制されない**。アプリ層の規約 |
| APIエンドポイント（api_endpoint） | `str(max_length=200) \| None` | nullable | **事実上 "hacker-news" \| "alpha-vantage" の2値だが自由文字列**。コードは固定分岐 |
| 収集間隔（fetch_interval_minutes） | `int` NOT NULL, default=720 | スキーマ側で 15〜1440 に制限 | **モデル側に制約なし。0やマイナスが入りうる**。0だとスケジューラが無限ループ相当 |
| 有効フラグ（is_active） | `bool` NOT NULL, default=True | フェッチ時に is_active=True のみ対象 | 無効化のトリガーは手動のみ。自動無効化は未実装 |
| 連続エラー数（consecutive_errors） | `int` NOT NULL, default=0 | マイナス値の防御なし | サーキットブレーカー的に使用されるが、**閾値が未定義** |
| 最終エラーメッセージ（last_error_message） | `str \| None` | nullable。長さ制限なし | デバッグ用の文字列 |
| ETag（etag） | `str(max_length=256) \| None` | nullable。RSS固有 | HTTP conditional GET 用キャッシュ |
| Last-Modified（last_modified_header） | `str(max_length=256) \| None` | nullable。RSS固有 | HTTP conditional GET 用キャッシュ |
| 次回フェッチ予定（next_fetch_at） | `datetime(tz=True) \| None` | nullable | スケジューラがセット |
| 最終フェッチ日時（last_fetched_at） | `datetime(tz=True) \| None` | nullable | フェッチ成功時に更新 |
| 信頼度 | — | **未実装** | consecutive_errors が間接指標だが、信頼度ランク付けの仕組みはない |

#### 深掘りすべき問い

- **ソースの追加・削除はどう管理する？**
  → 管理者のみAPI経由で CRUD。削除時は記事の source_id が SET NULL（記事自体は残る）
- **ソースごとに収集ルールが異なるか？**
  → source_type (RSS/API) で分岐。RSS は feedparser + conditional GET（ETag/Last-Modified）。API は api_endpoint 文字列でハードコード分岐（"hacker-news", "alpha-vantage"）
- **信頼度のランク付けは必要か？**
  → 現在未実装。consecutive_errors が間接的な信頼度指標だが、閾値によるソース自動無効化は未実装

#### 暗黙のルール

- `source_type == "rss"` なら `feed_url` が必須という制約は docs の CHECK 制約に記載あるが、モデル層では nullable のまま
- `api_endpoint` は自由文字列だが、news_fetcher.py では "hacker-news" と "alpha-vantage" 以外は `Unsupported API endpoint` エラー
- `fetch_interval_minutes` のデフォルト 720（12時間）は設定値だが、最小15分の制約はスキーマ層のみ
- RSS 以外のソースには etag / last_modified_header は無意味だが、NULL 許容で共存している（Discriminated Union 的だが型レベルでは未分離）

---

### 企業（Company）※未実装

| 項目 | 内容 |
|------|------|
| 概念の説明 | ニュースに登場するテック企業 |
| 現在の実装 | 未実装。ただし関連基盤は部分的に存在（後述） |

#### 既存の関連実装

- **Alpha Vantage API クライアント** (`backend/app/services/alpha_vantage.py`) — 現在はニュースフェッチにのみ使用。株価取得API (`GLOBAL_QUOTE`, `TIME_SERIES_DAILY`) は未使用
- **キーワードに企業名が混在** — Keyword モデルに "NVIDIA", "Tesla" 等の企業名がシードデータとして含まれるが、企業エンティティとしてモデル化されていない
- **AI分析プロンプト** — 企業名抽出の指示は含まれていない

#### 属性の棚卸し

| 属性名 | 現在の型 | ドメイン上の制約 | 問題点・メモ |
|--------|----------|-----------------|-------------|
| 企業名 | — | 正式名称と略称の扱いは？（例: Alphabet / Google） | 正規化のためのマスターデータ設計が必要 |
| ティッカーシンボル | — | フォーマットは？市場ごとに異なるか？ | 同一ティッカーが異なる市場に存在しうる（例: 7203.T と TM） |
| 市場 | — | NYSE, NASDAQ, 東証など。どこまで対応する？ | ティッカー + 市場の複合キーが必要か？ |
| 株価 | — | リアルタイム？遅延？通貨は？ | Alpha Vantage 無料プランは 5 calls/min, 500/day。リアルタイムは不可能 |

#### 深掘りすべき問い

- **ニュース本文から企業名をどう抽出する？**
  → AI 分析フローに組み込むか、別パイプラインとするか？現在の AI 分析プロンプトには企業抽出の指示がない
- **同じ企業の表記ゆれはどう統一する？**
  → "Alphabet Inc." / "Google" / "GOOGL" — これらを同一企業として扱うためのマスターデータ設計は？エイリアステーブルが必要か？
- **非上場企業も扱うか？**
  → テックニュースにはスタートアップが頻出。ティッカーのない企業をどう識別するか？
- **企業情報の更新頻度は？**
  → 株価は Alpha Vantage の制約上、日次〜数時間ごとが限界。「最終更新日時」を保持して鮮度を明示すべきか？
- **企業とキーワードの関係は？**
  → 現在の Keyword に企業名が混在している。企業を独立エンティティにした場合の棲み分けは？例: キーワード "NVIDIA" は「技術トピック」か「企業」か？
- **企業と記事のリレーションは？**
  → 1記事に複数企業が登場しうる。多対多が必要。現在の `NewsKeyword` 中間テーブルと同じパターンか？
- **Alpha Vantage との連携設計は？**
  → 現在はニュースフェッチにのみ使用。株価データ取得との連携をどう設計するか？企業エンティティの作成がトリガーか、キーワード登録がトリガーか？

---

### AI分析結果（Analysis）

| 項目 | 内容 |
|------|------|
| 概念の説明 | AIがニュース記事を分析した結果（翻訳・要約・センチメント・インパクトスコア） |
| 現在の実装 | `backend/app/models/analysis.py` → `AnalysisResult(SQLModel)` + `AnalysisTranslation(SQLModel)` |

#### 属性の棚卸し

| 属性名 | 現在の型 | ドメイン上の制約 | 問題点・メモ |
|--------|----------|-----------------|-------------|
| センチメント（sentiment） | `Sentiment(StrEnum)` NOT NULL | "positive" \| "negative" \| "neutral" の3値 | 既に値オブジェクト的。DB側はCHECK制約なし |
| インパクトスコア（impact_score） | `int` NOT NULL, ge=1, le=10 | 1〜10の整数 | **Pydantic と `AnalysisData.__post_init__` の二重検証だが、DB側にCHECK制約なし** |
| 分析理由（reasoning） | `str \| None` | nullable。長さ制限なし | AI出力のまま。サニタイズ（strip_html_tags）は適用済み |
| 分析日時（analyzed_at） | `datetime(tz=True)` NOT NULL | default=now(UTC) | 分析実行時刻 |
| 記事ID × AIモデルID | int (FK) | UNIQUE(news_article_id, ai_model_id) | 同一記事×同一モデルの重複分析を防止。記事削除時CASCADE、モデル削除はRESTRICT |
| 翻訳タイトル（translation.title） | `str(max_length=500)` NOT NULL | AI生成の日本語翻訳 | `strip_html_tags` 後、`or ""` で空文字フォールバック。**空文字が入りうる** |
| 翻訳要約（translation.summary） | `Text` NOT NULL | AI生成の日本語要約 | **長さ制限なし**。最大長が未定義 |
| ロケール（translation.locale） | `str(max_length=10)` NOT NULL | 現在は "ja" 固定 | 将来の多言語対応を想定。**自由文字列で列挙型ではない** |
| 投資カテゴリ | 中間テーブル経由 | UNIQUE(analysis_id, category_id) | AIが6種のスラグから1〜3個選択。**選択0個も可能（下限制約なし）** |
| キーワード | 中間テーブル（NewsKeyword）経由 | UNIQUE(news_article_id, keyword_id) | AIがシードキーワードから選択してタグ付け。**上限なし** |
| 関連企業 | — | **未実装** | AI分析プロンプトに企業抽出の指示がない |
| 分析モデル（ai_model） | FK → ai_models | RESTRICT on delete | provider + name のUNIQUE制約。モデル変更時は別レコードとして管理 |

#### 深掘りすべき問い

- **分析結果が不正確だった場合の修正フローは？**
  → 修正フローは存在しない。既存分析がある場合は `analyze_article` でスキップされる。手動修正のUIもAPIもない
- **分析のやり直しは可能か？**
  → UNIQUE(article_id, model_id) 制約により、同一モデルでのやり直しにはレコード削除が必要。異なるモデル（`evaluation_ai_model_id`）での再分析は可能。ただし再分析のAPIエンドポイントは未実装
- **分析未完了の状態は存在するか？**
  → 記事に AnalysisResult が存在しない = 未分析。明示的なステータスフィールドはない（例: "pending", "in_progress", "completed", "failed" のような enum がない）

#### 暗黙のルール

- `impact_score` の 1-10 範囲はドメインルールだが、DB の CHECK 制約では守られていない。直接SQL操作で範囲外の値が入りうる
- 翻訳の `title` が空文字 `""` になりうる（`strip_html_tags(data.title) or ""` のフォールバック）
- 投資カテゴリは6個のシードデータで固定（competitive_edge, financial_signal, growth_catalyst, market_disruption, regulatory_shift, risk_mitigation）。追加・変更はAlembicマイグレーション経由
- AI分析の入力に `content`（全文）が含まれるかは記事の content_fetched_at の有無次第 — **全文なしの分析と全文ありの分析が品質的に異なるが、区別されない**
- 分析間のリクエスト間隔は `analysis_request_interval=4.0秒`（約15 RPM）でレート制限

---

### キーワード（Keyword）

| 項目 | 内容 |
|------|------|
| 概念の説明 | ニュース記事のタギングとユーザー購読に使用される検索キーワード |
| 現在の実装 | `backend/app/models/keyword.py` → `Keyword(SQLModel)` |

#### 属性の棚卸し

| 属性名 | 現在の型 | ドメイン上の制約 | 問題点・メモ |
|--------|----------|-----------------|-------------|
| キーワード文字列（keyword） | `str(max_length=200)` UNIQUE, NOT NULL | スキーマ側でホワイトリスト検証（`[\w \-\.&/+#]+`）、min_length=1 | 空文字防止あり。**大文字小文字の正規化ルールがない**（"AI" と "ai" は別として登録可能） |
| カテゴリ | 中間テーブル → KeywordCategoryLink | UNIQUE(keyword_id, category_id) | 0個以上のカテゴリに所属可能。**上限なし** |

#### 深掘りすべき問い

- **キーワードは誰が管理するか？**
  → 管理者のみAPI経由で作成可能（`get_admin_user` 依存）。ユーザーは購読のみ
- **キーワードの役割は？**
  → 二つの役割がある: (1) AI分析時にカテゴリ付きでAIに提示 → AIが記事に関連するものを選択してタグ付け、(2) ユーザーが購読して興味あるニュースをフィルタリング
- **シードデータの管理は？**
  → 72件のシードデータがAlembicマイグレーションで投入済み。追加はAPI経由
- **キーワードと企業名の境界は？**
  → 現在は混在している。Company エンティティ導入時に分離が必要になる可能性あり

#### 暗黙のルール

- AI分析時に**全キーワード**をカテゴリ付きでAIに提示している（キーワード数が増えるとプロンプトが肥大化する）
- 大文字小文字の正規化がない — UNIQUEだが case-sensitive（PostgreSQLデフォルト）
- キーワード削除時は NewsKeyword と UserKeywordSubscription が CASCADE で連鎖削除される

---

### キーワードカテゴリ（KeywordCategory）

| 項目 | 内容 |
|------|------|
| 概念の説明 | キーワードを分類するカテゴリ（10個のシードデータ） |
| 現在の実装 | `backend/app/models/keyword_category.py` → `KeywordCategory(SQLModel)` + `KeywordCategoryTranslation(SQLModel)` |

#### 属性の棚卸し

| 属性名 | 現在の型 | ドメイン上の制約 | 問題点・メモ |
|--------|----------|-----------------|-------------|
| スラグ（slug） | `str(max_length=50)` UNIQUE, NOT NULL | カテゴリ識別子 | 英語小文字のスラグ。フォーマットの検証なし |
| 翻訳名（translation.name） | `str(max_length=100)` NOT NULL | UNIQUE(category_id, locale) | 多言語対応 |
| ロケール（translation.locale） | `str(max_length=10)` NOT NULL | 現在は "ja" / "en" | 列挙型ではなく自由文字列 |

#### 暗黙のルール

- 10個のシードデータで固定。動的な追加APIは未実装
- AI分析のプロンプトでキーワードをカテゴリ別にグループ化して提示するため、カテゴリ構成がAI分析の品質に影響する

---

### 投資カテゴリ（InvestmentCategory）

| 項目 | 内容 |
|------|------|
| 概念の説明 | AI分析結果に付与される投資観点の分類（6個固定） |
| 現在の実装 | `backend/app/models/investment_category.py` → `InvestmentCategory(SQLModel)` + `InvestmentCategoryTranslation(SQLModel)` |

#### 属性の棚卸し

| 属性名 | 現在の型 | ドメイン上の制約 | 問題点・メモ |
|--------|----------|-----------------|-------------|
| スラグ（slug） | `str(max_length=50)` UNIQUE, NOT NULL | 6個の固定値 | competitive_edge, financial_signal, growth_catalyst, market_disruption, regulatory_shift, risk_mitigation |
| 翻訳名（translation.name） | `str(max_length=100)` NOT NULL | UNIQUE(category_id, locale) | 多言語対応 |
| 翻訳説明（translation.description） | `Text \| None` | nullable | カテゴリの詳細説明 |

#### 深掘りすべき問い

- **6個のカテゴリは十分か？**
  → AI分析のプロンプトで1〜3個選択する指示。カテゴリの追加・変更はAlembicマイグレーションが必要
- **カテゴリの粒度は適切か？**
  → "market_disruption" と "growth_catalyst" の境界が曖昧。AIの選択精度に影響しうる

#### 暗黙のルール

- シードデータで固定。追加APIは未実装
- AIが分析時に投資カテゴリのスラグをそのまま使う — スラグの変更はAI分析のプロンプトにも影響する
- 1つの分析に0個〜N個のカテゴリを付与可能（下限・上限ともにDB制約なし）

---

### 重複グループ（ArticleGroup）

| 項目 | 内容 |
|------|------|
| 概念の説明 | 意味的に類似した記事をグループ化し、代表記事を選出する仕組み |
| 現在の実装 | `backend/app/models/article_group.py` → `ArticleGroup(SQLModel)` |

#### 属性の棚卸し

| 属性名 | 現在の型 | ドメイン上の制約 | 問題点・メモ |
|--------|----------|-----------------|-------------|
| 代表記事ID（canonical_id） | `int \| None` FK → news_articles | SET NULL on delete | **代表記事が削除されると SET NULL で代表なしのグループが残る** |
| 記事数（article_count） | `int` NOT NULL, default=1 | マイナス値の防御なし | **非正規化フィールド。実際のリレーション数と乖離しうる** |

#### 深掘りすべき問い

- **重複の基準は適切か？**
  → コサイン距離 < `dedup_similarity_threshold=0.15` かつ ±`dedup_time_window_days=3`日。閾値の調整は `config.py` で可能
- **代表記事の選出基準は？**
  → 最古の公開日 > コンテンツ有無 > 最高インパクトスコアの優先順。公開日が全てNoneの場合の挙動は？

#### 暗黙のルール

- グループの最小サイズは2（1記事だけのグループは作られない設計だが、記事削除で1になりうる）
- canonical_id の記事が削除されると SET NULL — **代表記事なしのグループが残る**
- article_count は非正規化フィールドで、記事の追加・削除時に手動で更新される。トランザクション外での不整合リスクあり
- 一度グループ化された記事を**グループから解除する仕組みがない**

---

### ユーザー購読（UserKeywordSubscription）

| 項目 | 内容 |
|------|------|
| 概念の説明 | ユーザーが関心のあるキーワードを購読し、関連ニュースをフィルタリングする |
| 現在の実装 | `backend/app/models/user_keyword.py` → `UserKeywordSubscription(SQLModel)` |

#### 属性の棚卸し

| 属性名 | 現在の型 | ドメイン上の制約 | 問題点・メモ |
|--------|----------|-----------------|-------------|
| ユーザーID（user_id） | `VARCHAR(32)` NOT NULL, index | Better Auth の cuid | **FK制約なし**（authスキーマとの分離のため論理参照） |
| キーワードID（keyword_id） | `int` FK → keywords | CASCADE on delete | キーワード削除で購読も消える |
| 購読日時（created_at） | `datetime(tz=True)` NOT NULL | default=now(UTC) | |

#### 暗黙のルール

- **購読数に上限なし**。大量購読によるパフォーマンス影響は未検討
- ユーザー削除時に購読が孤立する（Better Auth 側の user 削除は auth スキーマで完結し、public スキーマの user_id は論理参照のため連鎖削除されない）
- UNIQUE(user_id, keyword_id) で同一キーワードの二重購読は防止

---

### ウォッチリスト（WatchlistItem）

| 項目 | 内容 |
|------|------|
| 概念の説明 | ユーザーが気になる記事を保存するブックマーク機能 |
| 現在の実装 | `backend/app/models/watchlist.py` → `WatchlistItem(SQLModel)` |

#### 属性の棚卸し

| 属性名 | 現在の型 | ドメイン上の制約 | 問題点・メモ |
|--------|----------|-----------------|-------------|
| ユーザーID（user_id） | `VARCHAR(32)` NOT NULL, index | Better Auth の cuid | **FK制約なし**（論理参照） |
| 記事ID（news_article_id） | `int` FK → news_articles | CASCADE on delete | 記事削除でウォッチリストからも消える |
| 追加日時（created_at） | `datetime(tz=True)` NOT NULL | default=now(UTC) | |

#### 暗黙のルール

- **ウォッチリスト件数に上限なし**
- ユーザー削除時にウォッチリストが孤立する（購読と同じ問題）
- UNIQUE(user_id, news_article_id) で同一記事の二重保存は防止

---

## リファクタリング優先度

> 『セキュア・バイ・デザイン』の観点で、不正な値が入った場合の被害の大きさで優先順位をつけた。

| 優先度 | 概念 | 理由 |
|--------|------|------|
| 高 | impact_score | DB側にCHECK制約なし。1-10の範囲外が入るとフロントエンド表示崩壊・投資判断に影響。値オブジェクト化の第一候補 |
| 高 | url / feed_url | 不正URLがDBに入るとXSSリスク。スキーマ層のみの検証で、直接DB操作時に守られない。値オブジェクト `SafeUrl` の候補 |
| 高 | title_original | 空文字がNOT NULL制約をすり抜ける。タイトルなしの記事はUIで意味をなさない |
| 中 | source × source_id | 二重管理。source_id削除後にsource文字列が残り整合性が壊れる。source_id からのJOINに一本化すべき |
| 中 | fetch_interval_minutes | モデル層に0やマイナスの防御がない。0だとスケジューラが無限ループ相当 |
| 中 | api_endpoint | 自由文字列だが実質2値。列挙型にすべき |
| 中 | article_count（非正規化） | 実際の記事数と乖離しうる。整合性チェックの仕組みがない |
| 中 | user_id（孤立リスク） | FK制約なしの論理参照。ユーザー削除時にゴミデータが残る |
| 低 | consecutive_errors | 閾値が未定義。サーキットブレーカーの発動条件が不明確 |
| 低 | locale | "ja"固定だが型は自由文字列。将来の多言語対応時に問題になる可能性 |
| 低 | keyword 大文字小文字 | case-sensitive UNIQUE。正規化ルールの不在 |
