# テーブル分離: NewsArticle → discovered_articles + articles

> ステータス: 設計確定（実装待ち）

## 意思決定

現在の `NewsArticle` テーブルを `discovered_articles`（収集記録）と `articles`（分析対象）の2テーブルに分離する。

## 解決する問題

`NewsArticle` はメタデータ（RSS 由来）と分析入力（本文）が同居しており、`original_content` が NULL になりうる。テーブルの1行が「分析可能な記事」なのか「メタデータだけ取得した記録」なのかが曖昧で、`skip_content_fetch` フラグで状態を表現している。

分析サービス側では記事が分析に進める状態かをランタイムでチェックする必要があり、その判断ロジックが散逸している。

## 分離の根拠

メタデータと本文はデータとしての性質が異なる。

| | メタデータ | 本文 |
|---|---|---|
| 取得元 | RSS フィード（構造化、信頼できる） | Web スクレイピング（非構造化、失敗しうる） |
| 取得コスト | 軽い（フィード一括） | 重い（記事ごとに HTTP） |
| 失敗頻度 | ほぼない | よくある（JS 要求、paywall、抽出失敗） |
| 存在タイミング | 収集直後 | 後段のタスクで取得 |

タイミングと信頼性が違うものを同じ行に混ぜることで NULL 許容が増え、DB の意味が曖昧になっている。

## テーブル設計

### discovered_articles — RSS で発見した記録

| フィールド | 制約 | 役割 |
|---|---|---|
| `id` | PK | |
| `news_source_id` | FK, NOT NULL | どのソースから発見したか |
| `original_url` | UNIQUE, NOT NULL | 重複排除の基盤 |
| `original_title` | NOT NULL | 運用可視性（何が配信されているか） |
| `discovered_at` | NOT NULL, default now | 発見時刻 |

- `original_description` は持たない。articles 作成時に `original_content` に統合するため、メタデータ側に生値を残す必要がない
- `published_at` は持たない。記事の属性として articles 側に配置する
- `content_unreachable` フラグは持たない。URL の UNIQUE 制約により同じ URL は2度と INSERT されず、push 型パイプラインでは未取得記事を再スキャンするポーリングが存在しないため、フラグでフィルタする場面がない

### articles — 分析対象の記事

| フィールド | 制約 | 役割 |
|---|---|---|
| `id` | PK | |
| `discovered_article_id` | FK, UNIQUE, NOT NULL | 出自への参照 |
| `original_title` | NOT NULL | 分析入力 |
| `original_content` | NOT NULL | 分析入力（description 統合済み） |
| `published_at` | nullable | 記事の公開日時 |
| `created_at` | NOT NULL, default now | 分析対象になった日時 |

- `original_title` は `discovered_articles` にも存在するが、分析パイプラインが収集テーブルの存在を知るべきではないため、articles 側にも持つ。title は不変（一度セットされたら変わらない）のため更新異常のリスクがない
- `original_content` に RSS の description を統合する。content が NOT NULL で保証される以上、description を独立フィールドとして持つ意味がない。content に含めることで分析入力が title と content の2つに集約される
- `published_at` は nullable を許容する。取得できないケースがあるが、分析の成立条件には含めない（本文の有無と質のみ）

## 成立する不変条件

**articles 行が存在する = 分析可能。**

- `original_content` が NOT NULL のため、行がある時点で本文は必ず存在する
- 品質はファクトリメソッドで articles 作成時に強制する。ランタイムの readiness チェック（`is_ready_for_analysis` 等）は不要
- 分析サービスは「行がある = 分析可能」を前提として動き、冪等性チェック（`is_already_analyzed`）だけを行う

## パイプラインの変更

### 変更前

```
fetch_source_metadata
  → NewsArticle に保存（content は NULL の場合あり）
  → content + published_at があれば extract_content に直行
  → なければ fetch_content → extract_content
```

### 変更後

```
fetch_source_metadata
  → discovered_articles に保存
  → 全件 fetch_content をキューに投入

fetch_content
  → 本文を取得（RSS 由来 or スクレイピング）
  → 品質を満たすなら articles 行を作成
  → extract_content をキューに投入

extract_content → classify_content → generate_embedding
  （articles テーブルを起点に動く）
```

- RSS が content を提供している場合でも `fetch_content` を経由する。articles 行の作成箇所を一本化し、品質チェックを一箇所で行うため
- `fetch_source_metadata` の「content があれば分析に直行」分岐は消える

## 消えるもの

- `NewsArticle.original_content` の nullable — articles では NOT NULL
- `NewsArticle.skip_content_fetch` フラグ — テーブル分離で状態表現が不要に
- `NewsArticle.original_description` フィールド — content に統合
- 分析サービスの readiness チェック — 行の存在が保証
- `fetch_source_metadata` の分析直行分岐 — 全件 `fetch_content` 経由に統一
