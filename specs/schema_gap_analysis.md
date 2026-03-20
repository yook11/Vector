# ドメインモデル vs 現行スキーマ ギャップ分析

> 作成日: 2026-03-20
> ソース: `specs/db-domain-model.md`（ドメイン概念モデル）と `specs/db-current-schema.md`（現行スキーマ棚卸し）

## 1. エンティティ対応マッピング

| ドメインエンティティ | 現行DBテーブル | 対応状況 |
|---|---|---|
| NewsSource | news_sources | 部分一致 |
| NewsArticle | news_articles | 部分一致 |
| ArticleAnalysis | analyses + analysis_translations | 構造不一致 |
| Category | keyword_categories / investment_categories | 構造不一致 |
| Keyword | keywords | 属性欠落 |
| KeywordSynonym | （なし） | 未実装（想定通り） |
| Company | （なし） | 未実装（段階A） |
| CompanyAlias | （なし） | 未実装（段階A） |
| StockPrice | （なし） | 未実装（段階C） |
| TrendReport | （なし） | 未実装（想定通り） |
| User | auth.user | 一致 |
| WatchlistEntry | watchlists | 一致 |
| CustomFeed | （なし） | 未実装（想定通り） |

### DBにあるがドメインモデルにないもの

| 現行DBテーブル | 性質 | ドメインモデルでの扱い |
|---|---|---|
| article_groups | 重複記事のグループ化 | ドメイン概念として未定義 |
| fetch_logs | ソース取得のオペレーションログ | 運用詳細（ドメイン外で妥当） |
| ai_models | AIモデルの正規化テーブル | ドメインでは ArticleAnalysis.aiModel（文字列属性） |
| investment_categories | 投資カテゴリ（6種） | ドメインモデルに対応概念なし |
| user_keyword_subscriptions | ユーザーのキーワード購読 | ドメインモデルに対応概念なし（CustomFeed とも異なる） |
| analysis_translations | 分析結果の多言語翻訳 | ドメインでは ArticleAnalysis に直接属性 |
| keyword_category_translations | カテゴリの多言語翻訳 | ドメインでは Category.name のみ |
| investment_category_translations | 投資カテゴリの多言語翻訳 | 同上 |

---

## 2. 構造的ギャップ（要判断）

### GAP-1: Category の二重構造

**ドメイン**: 単一の `Category` エンティティ（セクター分類。AI/ML、半導体 等）
**現行DB**: 2つの独立したカテゴリ体系が存在

| DBテーブル | 用途 | リンク先 |
|---|---|---|
| keyword_categories | キーワードの所属セクター（10種） | keywords → keyword_category_links |
| investment_categories | 投資分析カテゴリ（6種） | analyses → analysis_investment_categories |

**不一致の本質**:
- `keyword_categories` はドメインの Category にほぼ対応する（セクター分類）
- `investment_categories` はドメインモデルに概念が存在しない。analyses に紐づく「投資観点タグ」（competitive_edge, growth_catalyst 等）であり、セクター分類とは異なる次元の分類
- ドメインモデルは investment_categories の概念を見落としているか、意図的に省略している

**結論: investment_categories は削除。keyword_categories を Category に昇格**

- `investment_categories` 関連テーブル3つ（investment_categories, investment_category_translations, analysis_investment_categories）を**削除する**
- ドメインモデルにも追加しない
- `keyword_categories` がドメインの `Category` に対応する。テーブル名を `categories` にリネーム

**削除の根拠**:
- 「AIの分析結果にAIが固定ラベルを貼る」構造であり、投資観点の主観性を隠蔽しているだけ
- 同じ記事でも「growth_catalyst か market_disruption か」は人によって異なる。固定タグによるフィルタは見かけほど有用ではない
- `impact_score` + `sentiment` + `reasoning` で投資観点は十分にカバーされている
- 将来 SNS センチメント等の外部データを取り込む段階で、その時のニーズに合った構造を設計する方が合理的

**削除対象（DB 再設計時に一括対応）**:
- テーブル: `investment_categories`, `investment_category_translations`, `analysis_investment_categories`
- モデル: `investment_category.py`
- スキーマ: `category.py` の CategoryBrief / CategoryResponse
- ルーター: `categories.py` 全体 + `news.py` の category フィルタ
- サービス: `gemini_analyzer.py` のプロンプト・パース + `ai_analyzer.py` の永続化
- フロントエンド: `CategoryBadge.tsx`, `NewsFilters.tsx` の投資カテゴリ部分, `api-client.ts`, 型定義
- テスト: `conftest.py` fixture, `test_news.py`, `test_ai_analyzer.py`

---

### GAP-2: Category ↔ NewsArticle の関係パス

**ドメイン**: Category ↔ NewsArticle は **直接の M:N**（中間テーブルで接続）
**現行DB**: **間接的な関係のみ**

```
DB: news_articles → news_keywords → keywords → keyword_category_links → keyword_categories
```

記事とカテゴリの間にキーワードが挟まっており、直接の中間テーブルがない。

**影響**:
- 「セクター別の記事一覧」を取得するクエリが3段JOINになる
- 記事にキーワードが付与されていなければ、カテゴリにも紐づかない
- ドメインモデルの意図（記事は直接セクターに分類される）と、実装（キーワード経由で間接的に分類される）が乖離

**結論: 間接関係（キーワード経由の導出）を正とする。ドメインモデル側を修正**

- `Category ↔ NewsArticle` の直接 M:N をドメインモデルから削除
- 記事のセクターは「AI分析でキーワードを抽出 → キーワード所属セクターから導出」で決まる
- ER図では導出関係（derived relationship）として注記で残す

**根拠**:
- 記事がどのセクターに属するかは、内容を分析して初めてわかる。取得段階では判断できない
- ソース（RSS）レベルの分類は不可能。総合テックメディアは全セクターの記事を配信しており、1ソース=1セクターにならない。RSSカテゴリタグもソースごとに定義がバラバラで制御不能
- 直接紐づけとキーワード経由の2系統が共存すると、矛盾時にどちらが正かわからなくなる（SSoT 違反）
- 3段JOINのパフォーマンスは記事数・カテゴリ数の規模では問題にならない。将来必要になればマテリアライズドビューやキャッシュカラムで対応可能

**現行DBへの影響**: なし（現行DBの構造が正しかった）

**ドメインモデルへの反映（要対応）**:
- ER図: `NewsArticle }o--o{ Category : "classified_in"` を削除し、導出関係の注記を追加
- 設計判断セクション: 上記根拠を記録
- 関係まとめ: 間接関係であることを明記

---

### GAP-3: ArticleAnalysis の多重度

**ドメイン**: NewsArticle → ArticleAnalysis は **1:1**（設計判断で明記）
**現行DB**: `analyses` テーブルの UNIQUE 制約は `(news_article_id, ai_model_id)` → **1記事あたりモデル数分の分析が可能（実質 1:N）**

**不一致の本質**:
- ドメインの設計判断では「モデル比較はユーザー機能ではなく内部検証用。YAGNI」として 1:1 と結論
- しかし DB は既に 1:N を許容する構造になっている
- 現在 ai_models は1件（gemini-2.0-flash）のみなので、実運用上は 1:1

**結論: DB を 1:1 に制約。ai_models テーブルを削除し文字列カラムに置き換え**

- `news_article_id` に UNIQUE 制約をかけ、1記事1分析を保証
- `ai_models` テーブルを削除し、`analyses.ai_model`（varchar）に置き換え（例: "gemini-2.0-flash"）
- UNIQUE制約の変更: `(news_article_id, ai_model_id)` → `(news_article_id)` のみ

**根拠**:
- ドメインの設計判断「モデル比較はユーザー機能ではなく内部検証用。YAGNI」に従う
- ai_models テーブルの正規化は現段階では過剰。モデル名の記録は文字列で十分（監査属性としての役割）
- 将来の拡張候補に「ArticleAnalysis の 1:N 拡張」は記載済み。必要時に Alembic で戻せる

**削除対象（DB 再設計時に一括対応）**:
- テーブル: `ai_models`
- FK: `analyses.ai_model_id` → `analyses.ai_model`（varchar）に変更
- UNIQUE制約: `uq_analyses_article_model` → `news_article_id` の単独 UNIQUE に変更
- インデックス: `idx_analyses_ai_model_id` 削除
- シードデータ: ai_models の初期データ不要に

---

### GAP-4: ArticleAnalysis の属性と翻訳パターン

**ドメイン**:
```
translatedTitle, translatedContent, summary, impactLevel(enum), aiModel(string), analyzedAt
```

**現行DB**:
```
analyses: sentiment(enum), impact_score(1-10), reasoning, ai_model_id(FK), analyzed_at
analysis_translations: locale, title, summary
```

| 属性 | ドメイン | DB | ギャップ |
|---|---|---|---|
| 翻訳タイトル | translatedTitle（直接属性） | analysis_translations.title | 多言語対応パターン（Translation Table）で分離 |
| 翻訳コンテンツ | translatedContent（直接属性） | **なし** | DB に翻訳コンテンツのカラムが存在しない |
| 要約 | summary（直接属性） | analysis_translations.summary | Translation Table で分離 |
| インパクト | impactLevel（enum） | impact_score（smallint 1-10） | 型が異なる（enum vs 数値スコア） |
| AIモデル | aiModel（string） | ai_model_id（FK → ai_models） | DB は正規化。ドメインは文字列 |
| センチメント | **なし** | sentiment（positive/negative/neutral） | ドメインモデルに概念がない |
| 推論理由 | **なし** | reasoning（text） | ドメインモデルに概念がない |

**結論: Translation Table 廃止、直接属性に簡素化。属性の取捨選択を確定**

| 属性 | 判断 | 理由 |
|---|---|---|
| translatedTitle | **残す（直接属性）** | analysis_translations を廃止し analyses テーブルに直接持たせる |
| translatedContent | **残す（直接属性）** | AI分析とは別パイプラインで機械翻訳API（Google Cloud Translation / DeepL等）を使用。LLMでの翻訳はリソースの無駄であり、機械翻訳APIなら低コストで十分な品質が得られる |
| summary | **残す（直接属性）** | 同上、analyses テーブルに直接持たせる |
| impactLevel / impact_score | **残す（形式は別途相談）** | enum vs 数値スコアの形式を決める |
| aiModel | **残す（文字列）** | GAP-3 で確定済み。ai_models テーブル削除、文字列カラムに |
| sentiment | **削除** | AIの主観ラベルであり、外部データ（SNSセンチメント等）の裏付けがない段階では有効でない。将来民意を反映できる段階で再追加する |
| reasoning | **追加（ドメインモデルに反映）** | impact_score の根拠説明として価値が高い。固定ラベルではなく自然言語なのでユーザーが自分で判断できる |

**DB再設計時の対応**:
- `analysis_translations` テーブル削除
- `analyses` テーブルに `translated_title`, `translated_content`, `summary`, `reasoning` を直接カラムとして持たせる
- `sentiment` カラム削除
- `keyword_category_translations` も廃止し、`categories` テーブルに `name_ja`, `name_en` 等で簡素化

**ドメインモデルへの反映（要対応）**:
- `reasoning`（推論理由）を ArticleAnalysis に追加
- `translatedContent` は維持（機械翻訳APIで別パイプライン実装）
- 将来の拡張候補に「sentiment の再追加（外部センチメントデータとの統合時）」を記載

---

### GAP-5: Keyword のライフサイクル属性欠落

**ドメイン**:
```
name, status(provisional/official/blacklisted), detectedAt, approvedAt
```

**現行DB**:
```
keyword(string, UNIQUE), created_at, updated_at
```

| 属性 | ドメイン | DB |
|---|---|---|
| name | name | keyword |
| status | provisional / official / blacklisted | **なし** |
| detectedAt | AI初回検出日時 | created_at（概ね対応） |
| approvedAt | 管理者承認日時 | **なし** |

**不一致の本質**:
- ドメインの承認ワークフロー（AI検出 → 暫定 → 管理者承認/マージ/削除）を DB が表現できない
- 現状の keywords は全て「正式」扱いで、ステータス区別なし
- KeywordSynonym テーブルも未作成のため、マージ操作も不可能

**ドメインモデルの実装段階注記**: 「基本機能は稼働中。承認ワークフロー・暫定タグUIは未実装」

**結論: DB再設計時にカラム追加。既存キーワードは official として移行**

- `keywords` テーブルに `status`（varchar or enum: provisional/official/blacklisted）、`approved_at`（timestamptz, nullable）を追加
- 既存の72件シードキーワードは管理者が事前定義したものなので `status = 'official'` として移行
- 今後AIが新規検出したキーワードのみ `provisional` として作成され、承認フローの対象となる
- `created_at` は `detectedAt` に概ね対応するのでそのまま活用
- ドメインモデルとの方針矛盾はなし。承認ワークフロー実装時に合わせて対応

---

### GAP-6: Keyword ↔ Category の多重度

**ドメイン**: Category → Keyword は **1:N**（キーワードは単一セクターに所属）
**現行DB**: `keyword_category_links` は **M:N 中間テーブル**（1キーワードが複数カテゴリに所属可能）

**ドメインの設計判断**: 「セクター横断性は記事側の M:N が吸収する。キーワードが1つに決められないなら、それはカテゴリ定義の問題」

**結論: 1:N に制約。中間テーブルを廃止し FK に置き換え**

- `keyword_category_links` 中間テーブルを削除
- `keywords` テーブルに `category_id`（FK → categories）を直接持たせる
- シードデータ72件は全て1カテゴリにのみ所属しており、M:N の実績なし

**根拠**:
- GAP-2 で「記事のセクターはキーワード所属セクターから導出する」と確定した。キーワードが複数セクターに属すと分類が曖昧に広がるリスクがある
- 記事が複数セクターにまたがるのは、複数キーワード（例: 「LLM」=AI/ML + 「GPUアーキテクチャ」=半導体）が付与されることで実現する。キーワード側の M:N は不要
- ドメインの設計判断「セクター横断性は記事側の M:N が吸収する」と整合

---

### GAP-7: article_groups のドメイン位置づけ

**ドメイン**: 概念なし
**現行DB**: `article_groups` テーブル + `news_articles.article_group_id`

- 類似記事（cosine distance ベース）をグループ化し、canonical_id で代表記事を指定
- 同一ニュースの複数ソース報道を束ねる機能

**結論: ドメインモデルに NewsEvent として追加。article_groups から NewsEvent への進化を目指す**

- 「現実世界で起きた1つの出来事」を表すドメイン概念として **NewsEvent** を定義する
- 複数メディアの報道を束ね、代表記事を提示しつつ、各メディアの視点（reasoning）を読み比べられる構造
- 報じているメディアの数自体が注目度のシグナルとなる

**ドメインモデルへの反映（要対応）**:
```
NewsEvent {
    datetime firstReportedAt "第一報の日時"
    int reportCount "報道メディア数"
}

NewsEvent ||--o{ NewsArticle : "has_reports"
NewsArticle の中で1つが canonical（代表記事）
```

**現段階の実装との距離**:
- 現在の `article_groups` は embedding cosine distance による機械的クラスタリング。「同じ出来事」を正確に捉えているとは限らない（類似トピックが混ざる可能性）
- ドメインモデルが実装をリードする形を取り、将来的にグループ単位の要約生成や精度向上で概念に近づけていく

**根拠**:
- 同じニュースでもメディアごとに論調が異なる。これは投資判断において重要な情報であり、単なる重複排除ではなく「多様な視点の提示」というドメインの価値に直結する
- 各記事の reasoning を読み比べることで論調の違いが可視化される（sentiment ラベルではなく自然言語で表現）
- 完全に1つに丸めると視点の多様性が消え、まとめないとノイズになる。NewsEvent 単位で束ねつつ中の記事を閲覧可能にするのが最適

---

### GAP-8: user_keyword_subscriptions のドメイン位置づけ

**ドメイン**: 概念なし（CustomFeed は未実装の別概念）
**現行DB**: `user_keyword_subscriptions`（user_id + keyword_id + created_at）

- ユーザーが特定キーワードを購読する機能
- CustomFeed はより汎用的（Category, Keyword, Company の組み合わせフィルタ）

**結論: CustomFeed 実装時に吸収・移行。ドメインモデルには追加しない**

- `user_keyword_subscriptions` は CustomFeed の簡易版・先行実装
- CustomFeed が上位概念であり、キーワード購読は CustomFeed の1ケース（Keyword だけを指定したフィード）として表現可能
- CustomFeed 実装時に既存の購読データを移行し、`user_keyword_subscriptions` テーブルは削除する

---

### GAP-9: NewsSource.importanceLevel の欠落

**ドメイン**: `importanceLevel`（ソースの信頼度・優先度）
**現行DB**: 該当カラムなし

- ドメインモデルでは「公式発表・大手メディアは個人ブログより優先される」記事トリアージの判断材料
- 現行DBには `is_active` や `fetch_interval_minutes` はあるが、信頼度・優先度の概念がない

**結論: NewsSource.importanceLevel をドメインモデルから削除**

- 信頼度はソースの属性ではなく、**記事の内容が持つ属性**。同じメディアでも「公式発表の報道」と「噂レベルの憶測記事」が混在する
- ソースレベルの信頼度管理は不要。信頼できるソースだけを収集対象にする設計（管理者がソースを選定）で十分
- 記事レベルの情報確度（official / confirmed / rumor 等）は ArticleAnalysis の属性として将来検討

**ドメインモデルへの反映（要対応）**:
- NewsSource から `importanceLevel` 属性を削除
- 将来の拡張候補に「ArticleAnalysis.reliability（記事の情報確度）」を記載

---

### GAP-10: NewsArticle のレガシーカラム

**現行DB**: `news_articles.source`（varchar100, NOT NULL）
- `source_id`（FK → news_sources）が追加された後も残存する旧カラム
- ドメインモデルには対応概念なし（NewsSource への FK のみ）

**結論: DB再設計時に削除**

- `news_articles.source` は `source_id`（FK → news_sources）導入前のレガシーカラム
- `source_id` が正規化された参照であり、文字列カラムは不要
- DB再設計で削除する

---

## 3. 確定済み結論サマリ

全10件のギャップについて設計判断が確定した。

### DB再設計時の変更一覧

**削除するテーブル:**
| テーブル | 理由 | GAP |
|---|---|---|
| investment_categories | AIの主観ラベル、有効でない | GAP-1 |
| investment_category_translations | 同上 | GAP-1 |
| analysis_investment_categories | 同上 | GAP-1 |
| ai_models | 過剰な正規化。文字列カラムで十分 | GAP-3 |
| analysis_translations | Translation Table 廃止、直接属性に | GAP-4 |
| keyword_category_translations | 同上 | GAP-4 |
| keyword_category_links | M:N → 1:N に変更、FK に置き換え | GAP-6 |
| user_keyword_subscriptions | CustomFeed に吸収 | GAP-8 |

**削除するカラム:**
| テーブル.カラム | 理由 | GAP |
|---|---|---|
| analyses.sentiment | 外部データなしでは有効でない | GAP-4 |
| analyses.ai_model_id | ai_models テーブル削除に伴い ai_model（varchar）に変更 | GAP-3 |
| news_articles.source | source_id 導入後のレガシー | GAP-10 |
| analyses.impact_score | enum impact_level（low/medium/high/critical）に型変更 | GAP-4 |

**追加するカラム:**
| テーブル.カラム | 内容 | GAP |
|---|---|---|
| analyses.translated_title | 翻訳タイトル（直接属性） | GAP-4 |
| analyses.translated_content | 翻訳コンテンツ（機械翻訳APIで生成） | GAP-4 |
| analyses.summary | 要約（直接属性） | GAP-4 |
| analyses.reasoning | 推論理由（ドメインモデルに追加） | GAP-4 |
| analyses.ai_model | AIモデル名（varchar） | GAP-3 |
| keywords.status | provisional / official / blacklisted | GAP-5 |
| keywords.approved_at | 管理者承認日時 | GAP-5 |
| keywords.category_id | FK → categories（1:N） | GAP-6 |

**リネーム:**
| 現行 | 新名称 | GAP |
|---|---|---|
| keyword_categories | categories（slug + name の2カラム構成。name_en は不要、多言語対応しないため） | GAP-1 |
| article_groups | news_events（概念を NewsEvent に昇格）。canonical_id FK は維持。article_count → report_count にリネーム。news_articles.article_group_id → news_event_id にリネーム | GAP-7 |

**UNIQUE制約の変更:**
| 対象 | 変更内容 | GAP |
|---|---|---|
| analyses | (news_article_id, ai_model_id) → news_article_id 単独 UNIQUE | GAP-3 |

### ドメインモデルへの反映一覧

| 変更 | 内容 | GAP |
|---|---|---|
| Category ↔ NewsArticle | 直接 M:N を削除。Keyword 経由の導出関係に変更 | GAP-2 |
| ArticleAnalysis | reasoning を追加。sentiment を削除。translatedContent を維持（機械翻訳API） | GAP-4 |
| ArticleAnalysis | 1:1 維持を明確化（DB側も制約） | GAP-3 |
| NewsSource | importanceLevel を削除 | GAP-9 |
| NewsEvent | 新エンティティとして追加（article_groups から昇格） | GAP-7 |
| investment_categories 関連 | ドメインに追加しない | GAP-1 |

### 将来の拡張候補に追記済み

| 候補 | トリガー | GAP |
|---|---|---|
| 外部センチメントデータの取り込み | SNS等の統合ニーズ | GAP-1 |
| sentiment 属性の再追加 | 外部データで民意を反映できる段階 | GAP-4 |
| ArticleAnalysis.reliability（情報確度） | 確度フィルタリングのニーズ | GAP-9 |

### 保留事項

なし（全件確定済み）

### 追加確定

| 項目 | 結論 |
|---|---|
| impactLevel の形式 | 数値スコア（1-10）を廃止し enum 4段階（low/medium/high/critical）に変更。判断基準の詳細は `docs/06_PROMPT_DESIGN.md` に定義 |
| news_articles.description_original | DBにカラムとして残す（AI分析・embedding生成で使用）。ドメインモデルには追加しない（パイプラインの実装詳細） |
| keywords.keyword → keywords.name | ドメインモデルと一致させるためリネーム。`keywords.keyword` は冗長 |

---

## 4. 次のアクション

1. **ドメインモデル（db-domain-model.md）を確定結論に基づいて改訂する**
2. **impactLevel の形式を決定する**（enum vs 数値スコア）
3. **新スキーマ設計書を作成する**（確定結論を反映した新しいDB設計）
