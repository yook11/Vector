# News Sources テーブル設計（セキュア・バイ・デザイン）

> 作成日: 2026-03-21
> ソース: `specs/db-domain-model.md` セクション 2.1 NewsSource
> ギャップ分析: GAP-9（importanceLevel 削除）

## 1. 概要

管理者が「ここから記事を取ってよい」と認めた情報源。10〜数十件程度の固定マスタ。
信頼性の担保は「管理者が収集対象ソースを選定する」ことで実現する。

### 現行 → 新設計の変更点

| 項目 | 現行 | 新設計 |
|------|------|--------|
| テーブル構成 | `news_sources` 1テーブルに全属性 | ドメイン属性 + 最小限のパイプライン設定に縮小 |
| `feed_url` / `api_endpoint` | 相互排他の2カラム（片方が必ずNULL） | `endpoint_url` に統合。`source_type` が解釈方法を決定 |
| `site_url` | NULL 許容 | NOT NULL に変更 |
| `name` | VARCHAR(200) | VARCHAR(50) に縮小 |
| `importanceLevel` | — | 削除済み（GAP-9。信頼度は記事内容の属性） |

### DB から除外したもの

| 属性 | 移行先 | 理由 |
|------|--------|------|
| `etag`, `last_modified_header` | Redis | HTTPキャッシュの一時状態。失われても1回分の余分な転送のみ。Redis が既にタスクキューブローカーとして稼働中 |
| `fetch_interval_minutes` | 環境変数/config | デプロイ設定。ソースごとに変える実需がない |
| `next_fetch_at` | ワーカー/Redis | スケジューラの一時状態 |
| `consecutive_errors`, `last_error_message` | ワーカー/ログ | 一時的なエラー状態 |
| `last_fetched_at` | `fetch_logs` から導出 | 取得日時は事実であり上書きすべきでない。取得履歴は `fetch_logs` が担う |

### テーブル分割について

当初 `news_sources`（ドメイン）と `source_feeds`（パイプライン）に分離する案があったが、パイプライン設定の大半をDB外に移した結果、残るのは `source_type`, `endpoint_url`, `is_active` のみ。1:1 で別テーブルにする意味がないため統合した。将来 1:N（1ソースに複数フィード）が必要になった時点で切り出す。

## 2. 属性の不変条件

### id

| 項目 | 定義 |
|------|------|
| 型 | Integer (AUTO INCREMENT) |
| DB制約 | PRIMARY KEY |
| 不変条件 | 自動採番、変更不可 |

### name

| 項目 | 定義 |
|------|------|
| 型 | VARCHAR(50) |
| DB制約 | `NOT NULL`, `UNIQUE`, `CHECK (char_length(trim(name)) >= 1 AND char_length(name) <= 50)` |
| 値オブジェクト | `NewsSourceName` |
| 不変条件 | ニュースメディアの名称。トリム後1文字以上、1-50文字 |
| 許可文字 | `^(?=.*\w)[\w \-\.]+$`（Unicode） |
| 根拠 | "TechCrunch", "Bloomberg L.P.", "Ars Technica" 等。ドット・ハイフン・スペースがあれば十分 |
| 備考 | 文字種制御は値オブジェクト（アプリ層）で実現。DB層は長さと非空のみ保証。理由: `\w` の挙動が PostgreSQL のロケール設定に依存するため、DB CHECK に正規表現を入れない |

### site_url

| 項目 | 定義 |
|------|------|
| 型 | VARCHAR(2048) |
| DB制約 | `NOT NULL`, `UNIQUE` |
| 値オブジェクト | `HttpUrl` |
| 不変条件 | メディアのトップページURL。http/https スキームのみ、パース可能な URL 形式、1-2048文字 |

### source_type

| 項目 | 定義 |
|------|------|
| 型 | VARCHAR(20) |
| DB制約 | `NOT NULL`, `CHECK (source_type IN ('rss', 'api'))` |
| 不変条件 | `endpoint_url` の解釈方法。2値の enum |

### endpoint_url

| 項目 | 定義 |
|------|------|
| 型 | VARCHAR(2048) |
| DB制約 | `NOT NULL`, `UNIQUE` |
| 値オブジェクト | `HttpUrl` |
| 不変条件 | 記事を取得しに行くURL。http/https スキームのみ、パース可能な URL 形式、1-2048文字 |
| 備考 | 現行の `feed_url`（RSS）と `api_endpoint`（API）を統合。どちらも「どこから取得するか」を表すURL であり、`source_type` が解釈方法を決定する。統合により相互排他の NULL カラム問題を解消 |

### is_active

| 項目 | 定義 |
|------|------|
| 型 | Boolean |
| DB制約 | `NOT NULL`, `DEFAULT true` |
| 不変条件 | false にするとフェッチ対象から外れる |

### created_at

| 項目 | 定義 |
|------|------|
| 型 | TIMESTAMP WITH TIME ZONE |
| DB制約 | `NOT NULL`, `DEFAULT NOW()` |
| 不変条件 | ソース登録日時。変更不可 |

### updated_at

| 項目 | 定義 |
|------|------|
| 型 | TIMESTAMP WITH TIME ZONE |
| DB制約 | `NOT NULL`, `DEFAULT NOW()` |
| 不変条件 | ソース情報の最終変更日時。全更新で自動更新 |
| 実現方法 | アプリ層（ORM イベント / 全テーブル共通の TimestampMixin）で自動更新。`DEFAULT NOW()` は INSERT 時のみ有効 |

## 3. エンティティレベルの不変条件

| 制約 | 実現レイヤー | 説明 |
|------|-------------|------|
| 管理者のみ CRUD 可能 | アプリ層（認可） | 一般ユーザーは参照のみ |
| NewsArticle が紐づく NewsSource は削除不可 | DB層（FK RESTRICT） | news_articles.news_source_id の ON DELETE RESTRICT |

## 4. 多層防御サマリ

| レイヤー | 防御内容 |
|----------|---------|
| **ドメイン層** | `NewsSourceName`（メディア名の文字種制約）、`HttpUrl`（URL形式・スキーム検証）|
| **DB層** | CHECK制約（source_type enum、name長さ）、UNIQUE（name, site_url, endpoint_url）、NOT NULL、FK RESTRICT |
| **アプリ層** | 値オブジェクトによるバリデーション、認可チェック（admin only） |

## 5. 値オブジェクト

### NewsSourceName

| 項目 | 定義 |
|------|------|
| ドメイン定義 | 管理者が登録するニュースメディアの名称 |
| 許可文字 | `^(?=.*\w)[\w \-\.]+$`（Unicode） |
| 長さ | 1-50文字（トリム後） |
| 例 | "TechCrunch", "Reuters", "Bloomberg L.P.", "Ars Technica" |

### HttpUrl（共通値オブジェクト）

| 項目 | 定義 |
|------|------|
| ドメイン定義 | HTTP/HTTPS でアクセス可能な Web リソースの所在 |
| 許可スキーム | `http`, `https` |
| 長さ | 1-2048文字 |
| 実装 | Pydantic v2 の組み込み `HttpUrl` 型をベースに、長さ制約を追加。既存の `validate_url_scheme` を置き換える |
| 使用箇所 | news_sources.site_url, news_sources.endpoint_url, news_articles.original_url 等、URLを扱う全箇所 |
