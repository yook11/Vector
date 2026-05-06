# フィード購読型への移行 — 現状調査指示書

## 目的

RSS フィード購読型への移行を計画している。実装に入る前に、現在のコードベースの関連箇所を調査し、影響範囲と変更点を正確に把握する。

**このタスクではコード変更を行わない。調査結果をレポートとして出力すること。**

---

## 背景

### 現在の方式（キーワード検索型）
- `keywords` テーブルのキーワードで Google News RSS を検索
- 例: `https://news.google.com/rss/search?q=quantum+computing`
- キーワード数だけ検索が走る

### 移行先（フィード購読型）
- 信頼できるメディアのカテゴリ別 RSS フィードを直接購読
- 新テーブル `rss_feeds` でフィード URL とカテゴリの紐づけを管理
- AI 分析時に `keywords` テーブルの小分類タグを記事に付与

---

## 調査項目

### 1. 記事取得パイプラインの現状

以下のファイルを読み、現在の取得フローを整理せよ:

- `backend/app/services/news_fetcher.py` — RSS 取得ロジック
- `backend/app/services/content_extractor.py` — 記事本文の抽出
- `backend/app/services/ai_analyzer.py` — AI 分析パイプライン
- `backend/app/taskiq_worker.py` — タスクキュー定義

報告すべき内容:
- Google News RSS の URL 構築方法
- 1回の取得でどのような処理が走るか（取得 → 本文抽出 → AI分析 → 保存の流れ）
- キーワードとの紐づけ方法（`news_keywords` テーブルへの INSERT ロジック）
- エラーハンドリング・リトライの仕組み
- `content_fetch_attempts` の扱い

### 2. DB スキーマの確認

以下のモデルファイルを読み、テーブル構造を確認せよ:

- `backend/app/models/news.py` — NewsArticle, NewsKeyword
- `backend/app/models/keyword.py` — Keyword
- `backend/app/models/keyword_category.py` — KeywordCategory, KeywordCategoryLink

報告すべき内容:
- `news_articles` テーブルのカラム一覧（特に `source` カラムの使われ方）
- `news_keywords` テーブルの構造（記事とキーワードの紐づけ方）
- `keyword_category_links` テーブルの構造
- 外部キー制約と CASCADE の設定

### 3. スケジューラの確認

- `backend/app/taskiq_worker.py` — 定期実行の設定
- `backend/app/config.py` — 関連する環境変数

報告すべき内容:
- 定期取得のスケジュール（間隔）
- タスク呼び出し時に渡されるパラメータ
- タイムアウト設定

### 4. API エンドポイントの確認

- `backend/app/routers/news.py` — `POST /news/fetch` の実装

報告すべき内容:
- 手動取得時のリクエストパラメータ
- `keyword_ids` の扱い
- 非同期タスクへのディスパッチ方法

### 5. フロントエンドの関連箇所

- `frontend/src/lib/api-client.ts` — `getNews()` の呼び出し
- `frontend/src/types/index.ts` — `NewsQuery` の定義

報告すべき内容:
- フロントエンドが記事取得時に使用するクエリパラメータ
- `source` フィールドの表示有無

---

## 出力フォーマット

以下の形式でレポートを出力せよ:

```markdown
# フィード購読型移行 — 現状調査レポート

## 1. 記事取得パイプライン
（調査結果）

## 2. DB スキーマ
（調査結果 + 簡易テーブル定義）

## 3. スケジューラ
（調査結果）

## 4. API エンドポイント
（調査結果）

## 5. フロントエンド
（調査結果）

## 6. 移行時の影響箇所まとめ
- 変更が必要なファイル一覧
- 新規作成が必要なファイル一覧
- 既存テーブルへの影響（カラム追加等）
- 削除・非推奨にすべき既存コード

## 7. 懸念事項・確認が必要な点
（設計判断が必要な箇所や潜在的な問題）
```

---

## 注意事項

- コード変更は行わない
- 推測ではなく、実際のコードを読んで報告する
- 「おそらくこうなっている」ではなく、該当コードの行番号やファイルパスを明記する
- 不明点があれば「確認が必要」と明記する
