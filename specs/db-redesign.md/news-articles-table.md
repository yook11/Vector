# News Articles テーブル設計（セキュア・バイ・デザイン）

> 作成日: 2026-03-22
> ソース: `specs/db-domain-model.md` セクション 2.2 NewsArticle
> ギャップ分析: GAP-10（source レガシーカラム削除）、GAP-7（article_group_id → ArticleAnalysis.news_event_id へ移動）

## 1. 概要

ドメインの中心エンティティ。英語の一次ソースから収集された記事。翻訳・分析・タグ付けの対象であり、ユーザーが閲覧する主要コンテンツ。
記事は収集された時点の事実であり、収集後に変更されない不変のエンティティ。

### 現行 → 新設計の変更点

| 項目 | 現行 | 新設計 |
|------|------|--------|
| `url` | VARCHAR(2048) | `original_url` にリネーム。ドメインモデルと一致 |
| `description_original` | TEXT | VARCHAR(2000) に変更。要約なので上限を設定 |
| `source_id` | NULLABLE, SET NULL | `news_source_id` NOT NULL, RESTRICT。全記事は必ずソースから来る |
| `source` | VARCHAR(100) | 削除（GAP-10。news_source_id に置き換え済みのレガシー） |
| `fetched_at` | 独立カラム | 削除。`created_at` で代替（レコード作成 = 収集） |
| `updated_at` | あり | 削除。記事は収集後に変更されない不変の事実 |

### DB から除外したもの

| 属性 | 移行先 | 理由 |
|------|--------|------|
| `guid` | Redis | パイプラインの重複排除キー。RSS 取得時に Redis で既知チェックし、`original_url` UNIQUE が最終的な安全網として機能する。Redis 再起動で失われても DB の UNIQUE 制約が重複を防ぐ |
| `content_fetch_attempts` | Redis キュー | リトライカウンタ。attempt をキューメッセージに持たせ、最大1回リトライ。失敗時は破棄し `description_original` がフォールバック。Redis 再起動で失われても分析は継続可能 |
| `content_fetched_at` | 不要 | `original_content IS NOT NULL` で取得済み判定可能。タイミングはログで追跡 |
| `embedding` | ArticleAnalysis | AI が記事内容から生成したベクトル表現。翻訳・要約と同じく分析の産物であり、記事そのものの事実ではない |
| `article_group_id` | ArticleAnalysis.news_event_id | embedding 類似度によるクラスタリング結果。どの出来事に属するかは分析して初めてわかることであり、未分析の記事は出来事に属さない |

### 外部データの扱いについて

Category, Keyword, NewsSource は管理者やシステムが作成する内部データであり、値オブジェクトで文字種を制限して「不正な値が存在できない」設計にできた。NewsArticle の属性は大半が外部ソースから取得するデータであり、こちらが文字種を制御できない。そのため、サニタイズ・バリデーションによる防御を採用する。

## 2. 属性の不変条件

### id

| 項目 | 定義 |
|------|------|
| 型 | Integer (AUTO INCREMENT) |
| DB制約 | PRIMARY KEY |
| 不変条件 | 自動採番、変更不可 |

### original_title

| 項目 | 定義 |
|------|------|
| 型 | VARCHAR(500) |
| DB制約 | `NOT NULL` |
| 不変条件 | ソースが公開した記事タイトル。外部データのためサニタイズ・バリデーションで対応。変更不可 |
| 備考 | 英語ニュースのタイトルは通常50〜150文字、長いもので250文字程度。500は安全マージン込みの上限 |

### description_original

| 項目 | 定義 |
|------|------|
| 型 | VARCHAR(2000) |
| DB制約 | NULLABLE |
| 不変条件 | ソースが公開した記事の要約・冒頭文。超過時は切り詰め。外部データのためサニタイズ・バリデーションで対応。変更不可 |
| 備考 | 全ソースが提供するとは限らないため NULL 許容。`original_content` が取得できなかった場合のフォールバックとして AI 分析・embedding 生成の入力に使用 |

### original_url

| 項目 | 定義 |
|------|------|
| 型 | VARCHAR(2048) |
| DB制約 | `NOT NULL`, `UNIQUE` |
| 値オブジェクト | `HttpUrl` |
| 不変条件 | 原文の所在URL。http/https スキームのみ、パース可能な URL 形式、1-2048文字。変更不可 |
| 備考 | 重複排除の最終的な安全網。`guid` の Redis チェックをすり抜けても、この UNIQUE 制約が同一記事の二重取り込みを防ぐ |

### original_content

| 項目 | 定義 |
|------|------|
| 型 | TEXT |
| DB制約 | NULLABLE |
| 不変条件 | trafilatura で取得した原文本文。外部データのためサニタイズ・バリデーションで対応。取得後は変更不可 |
| 備考 | RSS フィード取得（ステージ1）後に、別途 URL にアクセスして本文を抽出する（ステージ2）。取得失敗時は NULL のまま。リトライは Redis キューで管理（最大1回）。NULL の場合は `description_original` が AI 分析のフォールバック入力となる |

### published_at

| 項目 | 定義 |
|------|------|
| 型 | TIMESTAMP WITH TIME ZONE |
| DB制約 | NULLABLE |
| 不変条件 | ソースでの公開日時。変更不可 |
| 備考 | 全ソースが公開日時を提供するとは限らないため NULL 許容 |

### news_source_id

| 項目 | 定義 |
|------|------|
| 型 | Integer |
| DB制約 | `NOT NULL`, `FOREIGN KEY REFERENCES news_sources(id) ON DELETE RESTRICT` |
| 不変条件 | 記事の収集元ソース。全記事は必ず1つのソースから収集される。変更不可 |
| 備考 | RESTRICT により、記事が存在する NewsSource の削除を防止。ソースが不要になった場合は `is_active = false` で無効化する |

### created_at

| 項目 | 定義 |
|------|------|
| 型 | TIMESTAMP WITH TIME ZONE |
| DB制約 | `NOT NULL`, `DEFAULT NOW()` |
| 不変条件 | レコード作成日時 = 記事の収集日時。変更不可 |
| 備考 | 速報性の計測（`created_at - published_at`）に使用可能。`collected_at` / `fetched_at` を別カラムにしない理由: レコード作成と収集は同一のイベントであり、別名を持つ意味がない |

## 3. エンティティレベルの不変条件

| 制約 | 実現レイヤー | 説明 |
|------|-------------|------|
| 記事の不変性 | 設計原則 | 収集後のドメイン属性は変更されない。`updated_at` を持たない。`original_content` の後追い取得は「収集の完了」であり「更新」ではない |
| 外部データのサニタイズ | アプリ層 | `original_title`, `description_original`, `original_content` は外部データのため、保存時にサニタイズ・バリデーションを適用 |
| コンテンツ取得リトライ | Redis キュー | `original_content` の取得失敗時、attempt=0 でキュー末尾に追加。再失敗時に attempt=1 なら破棄。`description_original` がフォールバック |

## 4. 多層防御サマリ

| レイヤー | 防御内容 |
|----------|---------|
| **ドメイン層** | `HttpUrl`（original_url の URL 形式・スキーム検証） |
| **DB層** | UNIQUE（original_url）、NOT NULL（original_title, original_url, news_source_id, created_at）、FK RESTRICT（news_source_id） |
| **アプリ層** | 外部データのサニタイズ・バリデーション、`HttpUrl` によるURL検証 |
| **パイプライン層** | guid による Redis 重複排除（高速スキップ）、コンテンツ取得リトライ（Redis キュー） |

## 5. 設計判断の記録

| 判断 | 結論 | 理由 |
|------|------|------|
| `updated_at` の廃止 | 不要 | 記事のドメイン属性は収集後に変更されない。Keyword（status 遷移）や NewsSource（name 変更）とは性質が異なる |
| `collected_at` の廃止 | `created_at` で代替 | レコード作成 = 収集。別名を持つ意味がない |
| `guid` の DB 除外 | Redis に移行 | パイプラインの重複排除キー。`original_url` UNIQUE が最終安全網として機能するため DB に持つ必要がない |
| `description_original` の分類 | ドメイン属性 | ソースが記事と共に公開した要約であり、パイプラインが生成したものではなく記事そのものの事実 |
| `embedding` の移動 | ArticleAnalysis | AI が記事内容から生成したベクトル表現。記事の事実ではなく分析の産物 |
| `news_event_id` の移動 | ArticleAnalysis | どの出来事に属するかは embedding 分析で初めて決まる。未分析の記事は出来事に属さない |
| `content_fetch_attempts` の除外 | Redis キュー | リトライカウンタはパイプラインの一時状態。NewsSource の `consecutive_errors` と同じ原則 |
| 外部データの扱い | サニタイズ・バリデーション | 内部データ（Category 等）は値オブジェクトで文字種制限できるが、外部データは制御不可能。保存時の防御で対応 |
| `source_id` の NOT NULL 化 | NOT NULL + RESTRICT | 全記事は必ずソースから収集される。ソースを消したいが記事は残したいケースは存在しない（`is_active = false` で対応） |
