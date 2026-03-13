# Vector ロードマップ — Phase 3 以降（v7）

> v6 からの変更点:
>
> - Phase 3E-1（管理者ロール・アクセス制御）完了に更新
> - Gemini リトライ戦略改善・HTML サニタイズ追加を現状サマリーに反映
> - 実行順序サマリーを更新

> v5 からの変更点:
>
> - Phase 3B-1（重複記事検出）フロントエンド実装完了に更新
> - 実行順序サマリーを更新

> v4 からの変更点:
>
> - Phase 3B-1（重複記事検出）をバックエンド実装完了に更新
> - 実行順序サマリーを更新

> v3 からの変更点:
>
> - Phase 3A の進捗を反映（3A-1 完了、3A-2 完了、3A-3 部分完了）
> - Phase 3B の進捗を反映（3B-2 類似記事エンドポイント実装済み）
> - 問題点一覧（P-9, F-1, F-2, F-3）をロードマップに統合
> - 実行順序サマリーを更新

---

## 現状サマリー（2026-03-10 時点）

|項目|状態|
|---|---|
|基盤|Docker, FastAPI, Next.js, PostgreSQL+pgvector, Redis+taskiq|
|AI分析|gemini-2.5-flash-lite で稼働中（Tier 1 有効）|
|AI自動タグ付け|分析時にキーワード候補から最大3つ自動選択|
|Embedding|gemini-embedding-001（768次元）、重複記事検出パイプライン実装済み|
|ニュースソース|RSS 7件 + Hacker News API + Alpha Vantage API（HTML サニタイズ対応済み）|
|カテゴリ|10大分類 × 72小分類（キーワード）、サイドバードリルダウン実装済み|
|認証・認可|JWT 認証 + ロールベースアクセス制御（admin/user）|
|Billing|Tier 1 有効、予算アラート設定済み|
|Rate Limiter|REQUEST_INTERVAL は config 経由（`analysis_request_interval`）|
|安全策|トークン上限 5,000,000/日、RPD 5,000|
|Gemini リトライ|2層戦略: RPM 429 は retryDelay パース→待機リトライ、日次クォータ超過は即座に停止|

### 完了済みフェーズ

- [x] Phase 3-0: 技術的負債の解消（T-1〜T-4 全完了）
- [x] Phase 3A-1: ニュース取得範囲の拡大
- [x] Phase 3A-2: タグ・カテゴリシステムの拡充
- [x] Phase 3B-1: 重複記事検出（BE/FE 実装完了）
- [x] Phase 3E-1: 管理者ロール・アクセス制御

---

## ~~Phase 3-0: 技術的負債の解消~~ ✅ 完了

|タスク|内容|状態|
|---|---|---|
|T-1|taskiq timeout 600→1800|✅|
|T-4|content_fetch_attempts リトライ上限|✅|
|T-2|/news/fetch 非同期化|✅|
|T-3|backfill_decoded_urls 実行|✅|

---

## ~~Phase 3A-1: ニュース取得範囲の拡大~~ ✅ 完了

**達成内容**:

- RSS ソース 7件シード済み（TechCrunch, FierceBiotech, BioPharma Dive, The Quantum Insider, Cointelegraph, Yahoo Finance, ITmedia）
- Hacker News API 統合（Algolia HN Search、min_points フィルタ）
- Alpha Vantage News Sentiment API 統合（日次クォータ制限付き）
- ソース単位のフェッチスケジュール実装（`fetch_interval_minutes`, `next_fetch_at`）
- FetchLog テーブルでソース信頼性メタデータを記録
- ETag/Last-Modified によるコンディショナル GET 対応

**残課題**: RSS ソースの追加（Ars Technica, The Verge, Reuters Tech, Nikkei Asia 等）は運用しながら随時追加。

---

## ~~Phase 3A-2: タグ・カテゴリシステムの拡充~~ ✅ 完了

**達成内容**:

- `keyword_categories`（10大分類）+ `keywords`（72小分類）+ `keyword_category_links`（M:N）で階層表現
  - ※ 当初予定の `parent_id` 方式ではなく、M:N join テーブル方式を採用
- `keyword_category_translations` で多言語対応（ja/en）
- AI 分析時にキーワード候補からの自動タグ付け実装
- `GET /api/v1/keyword-categories` エンドポイント（locale パラメータ対応）
- `GET /api/v1/news` に `kwCategoryId` フィルタ追加
- フロントエンド: CategorySidebar でカテゴリドリルダウン UI 実装

---

## Phase 3A-3: タスク別モデル選定 — 部分完了

**完了済み**:

- `ai_models` テーブル作成・シード済み（gemini-2.5-flash-lite, id=1）
- `default_ai_model_id` / `evaluation_ai_model_id` の config 定義
- 分析レイヤーで `ai_model_id` パラメータ受け入れ可能

**未実装**:

- 評価モデルフローの実装（config は存在するがコード上で未参照）
- 比較用エンドポイント / スクリプト → **Phase 3F で管理画面として実装予定**

**方針**: 工程別（翻訳/Embedding/キーワード抽出）のモデル分けは行わず、「分析モデル全体」の単位で比較する。管理画面（3F）で評価ジョブを実行し、モデル切り替えの判断を行う。

---

## Phase 3B: 分析・UX 改善

### ~~3B-1. 重複記事の検出~~ ✅ 完了

**達成内容（バックエンド）**:

- `article_groups` テーブル + `news_articles.article_group_id` FK で重複グループ管理
- `backend/app/services/dedup.py`: pgvector コサイン距離による重複検出・グルーピングサービス
- タスクキューの Phase 6 として自動実行（embed 完了後に即座に重複検出）
- `GET /api/v1/news` に `deduplicated` パラメータ追加（デフォルト true: 代表記事のみ表示）
- `GET /api/v1/news/groups/{group_id}` エンドポイント新設
- `NewsResponse` に `duplicateCount`, `articleGroupId` フィールド追加
- 調査スクリプト `backend/scripts/investigate_similarity.py` 作成済み
- config: `dedup_similarity_threshold=0.15`, `dedup_time_window_days=3`

**達成内容（フロントエンド）**:

- `DuplicateBadge.tsx`: 「+N sources」バッジコンポーネント（クリックでダイアログ展開）
- `NewsCard.tsx`: ソース名・日付の横にバッジを表示（`duplicateCount > 0` の記事のみ）
- `api-client.ts`: `getGroupArticles(groupId)` 追加
- `generated.ts`: OpenAPI 型再生成で `duplicateCount`, `articleGroupId` 反映

**残タスク**:

- embedding backfill（Gemini API 日次クォータ超過のため翌日以降に自動実行）
- 調査スクリプトで閾値を検証・調整

### 3B-2. セマンティック検索

**現状**: 類似記事エンドポイント `GET /api/v1/news/{id}/similar` は実装済み（コサイン距離）。Embedding は gemini-embedding-001（768次元）で生成・保存済み。

未実装:

- HNSW インデックス作成（大規模化時のパフォーマンス向上）
- フリーテキスト検索 API エンドポイント（クエリ文をベクトル化して検索）
- フロントエンドの検索 UI

### 3B-3. ウォッチリスト強化

タグ・カテゴリ（3A-2 ✅）が完了したため着手可能。

### 3B-4. ソース信頼度スコアリング

FetchLog でエラー率・記事取得数は記録済み。信頼度スコアの算出ロジックは未実装。

### 3B-5. 通知・アラート

ウォッチリスト（3B-3）が揃ってから。

---

## Phase 3C: 株価連携

**yfinance 無料で PoC 可能。ソーシャルデータ（3D）より先に実装。**

> Alpha Vantage は現在ニュースフェッチに使用中。株価データ取得には別途対応が必要。

### 3C-1. 企業名抽出 (NER)

- AI 分析パイプラインに企業名・ティッカー抽出ステップを追加
- 出力: `mentioned_companies: [{name, ticker, exchange}]`
- 日本企業: 東証コード、米国企業: NASDAQ/NYSE ティッカー

### 3C-2. 株価データ取得

|API|料金|日本株|特徴|
|---|---|---|---|
|Yahoo Finance (yfinance)|無料|○|非公式だが広く利用|
|Alpha Vantage|無料枠あり|△|公式 API|
|J-Quants|有料|◎|東証公式データ|

推奨: yfinance で PoC → 本格運用で J-Quants に移行検討

### 3C-3. 株価 UI

- 記事詳細ページに「関連企業」セクション
- ミニチャート: 記事公開日前後の株価推移
- ニュースの市場インパクトを視覚化

---

## Phase 3D: ソーシャルデータ統合

**コスト大（X API: $100/月〜）のため、無料 API（Reddit）を優先検討。**

### 3D-1. データ収集

- Reddit API（無料枠）でテック系サブレディットからセンチメント収集
- X（旧Twitter）は Basic tier で PoC、本格導入はコスト見合い

### 3D-2. センチメント分析パイプライン

- ポストのセンチメント分析
- 既存のニュースセンチメントとの統合スコア算出

### 3D-3. 可視化

- 記事詳細ページに「ソーシャル反応」セクション
- センチメント分布チャート
- 代表的なポストの表示

---

## Phase 3E: 管理・運用基盤

### ~~3E-1. 管理者ロールとアクセス制御（旧 F-3）~~ ✅ 完了

**達成内容（バックエンド）**:

- User モデルに `role` カラム追加（`UserRole` StrEnum: `admin` / `user`、デフォルト `user`）
- Alembic マイグレーション `a9_add_role_to_users` で `role` カラム追加（`server_default="user"`）
- `get_admin_user()` FastAPI 依存関数を追加（403 Forbidden を返す）
- JWT payload に `role` クレームを追加
- `UserResponse` スキーマに `role` フィールド追加
- 管理者限定エンドポイント:
  - `POST /news/fetch`, `POST /news/embed`
  - `POST/PUT/DELETE/PATCH /sources`
  - `POST/PATCH/DELETE /keywords`
- GET 系エンドポイントは認証ユーザーであれば全員アクセス可能
- CLI スクリプト `scripts/promote_admin.py` で管理者昇格/降格（SSoT は DB の `role` カラム）

**達成内容（フロントエンド）**:

- NextAuth.js セッションに `role` を伝播（JWT → session）
- `FetchButton.tsx`: `session?.user?.role !== "admin"` の場合は非表示
- `next-auth.d.ts`: Session.user / JWT に `role` 型定義追加
- OpenAPI 型再生成で `UserResponse.role` 反映

**達成内容（テスト）**:

- `admin_user` / `admin_client` テストフィクスチャ追加
- 管理者限定エンドポイントのテストを `admin_client` に切り替え
- 既存テストの lint エラー修正（F841, F401, E501）

### 3E-2. content_fetch_attempts のリセット手段（旧 F-2）

**現状**: `content_fetch_attempts` カウンタと `content_max_fetch_attempts=3` の config は存在するが、リトライ上限のフィルタロジックが呼び出し側で明示的に実装されているか要確認。リセット用の管理手段はない。

実装方針:

- 管理者エンドポイント `POST /admin/articles/reset-fetch-attempts` を追加
- 対象: 全失敗記事 or 個別記事 ID 指定
- 3E-1（管理者ロール）が前提

### 3E-3. 表示言語の切り替え UI（旧 P-9）

**現状**: バックエンド/API は `locale` パラメータ対応済み。`keyword_category_translations` に ja/en データあり。フロントエンドの UI 切り替え機能が未実装。

実装方針:

- User モデルに `locale` カラム追加（デフォルト: `ja`）
- Settings ページに言語切り替え UI
- API リクエストに `locale` を自動付与
- 将来的に i18n フレームワーク（next-intl 等）の導入を検討

---

## Phase 3F: AI モデル評価管理画面

**前提**: 3E-1（管理者ロール・アクセス制御）✅ 完了済み。着手可能。

**目的**: 管理者が AI モデルの品質を実際のニュースデータで検証し、モデル切り替えの判断を行えるようにする。

### 3F-1. 評価ジョブ設定・単体実行

管理者画面から以下を設定して「テスト実行」できる機能。

- **使用モデル**: `ai_models` テーブルから1つ選択
- **取得件数**: 1回の評価で分析する記事数（例: 5件、上限設定あり）
- **対象ソース**: `news_sources` からチェックボックスで選択
- **実行**: 指定条件でニュース取得→分析を一括実行
- **結果表示**: 翻訳タイトル・要約・センチメント・impact_score・キーワードタグを一覧表示

用途: 新モデル導入前の動作確認、分析品質のスポットチェック。

### 3F-2. モデル比較（A/B テスト）

同一記事セットに対して **2モデルまで** の分析結果を並列比較する機能。

- **設定**: モデル A（現行）とモデル B（候補）を選択、取得件数・対象ソースは 3F-1 と共通
- **実行**: 同じ記事群を両モデルで分析し、結果を `analyses` テーブルに `ai_model_id` で区別して保存
- **比較ダッシュボード**:
  - 記事ごとの並列比較（翻訳品質、要約、センチメント、impact_score）
  - 集計指標: センチメント一致率、impact_score 差分の平均・分布
  - API コスト・レスポンス速度の比較（記録可能な場合）

### 実装メモ

- `analyses` テーブルは既に `UNIQUE(news_article_id, ai_model_id)` で1記事×複数モデルに対応済み
- 評価ジョブの実行履歴を管理するテーブル（`evaluation_jobs`）の追加を検討
- 評価実行は taskiq 経由の非同期ジョブとして実装（既存の fetch→analyze パイプラインを流用）

---

## Phase 4: 高度分析・将来構想

### 4-1. 要約密度・詳細度の選択

> v1 では Phase 3B だったが、ROI を考慮して優先度を下げた。

### 4-2. 多言語ソース対応

- 日本語ニュースソースの追加（日経等）
  - ※ ITmedia は 3A-1 で追加済み
- 海外 vs 国内の視点比較ダッシュボード

### 4-3. トピック別センチメントトレンド

- 特定トピックのセンチメント時系列グラフ

### 4-4. 分析結果の再処理自動化

- モデル変更時に既存記事を自動再分析するジョブ
- バージョニング: 分析結果にモデル名・バージョンを記録

### 4-5. AI によるキーワード自動拡充（旧 F-1）

**現状**: 72 小分類は手動管理。長期的にスケールしない。

実装方針:

- AI 分析時に既存キーワードに該当しない新概念を検出
- 候補として保存し、管理者が承認制で正式キーワードに昇格
- 3E-1（管理者ロール）が前提

---

## 実行順序サマリー

|#|フェーズ|内容|依存|状態|
|---|---|---|---|---|
|1|前提|Billing 有効化|—|✅ 完了|
|2|前提|Rate Limiter 緩和 + 一括処理|#1|✅ 完了|
|3|3-0|T-1〜T-4: 技術的負債解消|—|✅ 完了|
|4|3A-1|ニュース取得範囲拡大|—|✅ 完了|
|5|3A-2|タグ・カテゴリシステム|—|✅ 完了|
|6|3A-3|タスク別モデル選定|#4（品質検証後）|⏳ 基盤のみ|
|7|3B-1|重複記事検出（コサイン類似度）|#4 と並行|✅ 完了|
|8|3B-2|セマンティック検索（HNSW + 検索 UI）|—|⏳ 類似記事APIのみ|
|9|3E-1|管理者ロール・アクセス制御|—|✅ 完了|
|10|3F-1|AI 評価ジョブ設定・単体実行|#9|🔜 着手可|
|11|3F-2|モデル比較（A/B テスト）|#10|未着手|
|12|3B-3|ウォッチリスト強化|#5|未着手|
|13|3E-2|content_fetch_attempts リセット手段|#9|🔜 着手可|
|14|3E-3|表示言語切り替え UI|—|未着手|
|15|3C-1/2|企業名抽出 + yfinance PoC|#5|未着手|
|16|3C-3|株価 UI|#15|未着手|
|17|3B-4|ソース信頼度スコアリング|#4|未着手|
|18|3B-5|通知・アラート|#5, #12|未着手|
|19|3D|ソーシャルデータ統合|#15（コスト見合い）|未着手|
|20|4|要約密度・多言語・トレンド・AI キーワード拡充|Phase 3 全体|未着手|

---

## 開発原則（継続）

- **Specification-Driven Development**: 各フェーズでスペック文書を先に作成
- **EARS 構文**: 要件定義に使用
- **段階的検証**: 各機能は PoC → 品質検証 → 本番導入の流れ
- **既存資産の活用**: 新テーブル追加より既存モデルの拡張を優先
- **本番コードへの影響最小化**: 検証スクリプトは本番コードと分離
- **コスト意識**: API コストを常に試算してから実装に入る
- **技術的負債の早期解消**: 機能追加前に基盤を整える

---

## 評価モデルフロー（Phase 3F で実装）

> 以下は Phase 3F の管理画面で実現する。`.env` による手動切り替え運用は廃止し、管理画面から操作する方式に移行。

### Worker の変更（taskiq_worker.py）

管理画面からの評価ジョブ実行時、指定モデルで分析を行う。A/B 比較時は同じ記事群を2モデルで独立に分析し、一方の失敗がもう一方に影響しないようにする。

### Analyzer の拡張

評価モデルが Gemini の別バージョンであれば、既存の `gemini_analyzer.py` にモデル名を渡すだけで対応できる。別プロバイダー（OpenAI 等）の場合は、`BaseAnalyzer` を継承した新しい analyzer クラスを作成し、`get_analyzer()` ファクトリでプロバイダーに応じて切り替える。

### 比較ダッシュボード（3F-2）

管理画面上で同一記事に対する2モデルの分析結果を並列表示。センチメント一致率、impact_score 差分、キーワードタグ候補一致率を集計表示する。

### モデル切り替えの運用フロー

1. 管理画面で候補モデルを `ai_models` に登録
2. 3F-1 で単体テスト実行し、基本動作を確認
3. 3F-2 で現行モデルと A/B 比較
4. 品質に問題なければ管理画面から `DEFAULT_AI_MODEL_ID` を切り替え
