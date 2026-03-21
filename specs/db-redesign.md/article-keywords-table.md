# Article Keywords テーブル設計（セキュア・バイ・デザイン）

> 作成日: 2026-03-22
> ソース: `specs/db-domain-model.md` セクション 4 概念間の主要な関係（NewsArticle ↔ Keyword M:N）

## 1. 概要

NewsArticle と Keyword の M:N 関係を実現する中間テーブル。AI 分析が記事にキーワードをタグ付けした結果を保持する。

### 現行 → 新設計の変更点

| 項目 | 現行 | 新設計 |
|------|------|--------|
| テーブル名 | `news_keywords` | `article_keywords`（ドメインモデルと一致） |
| 主キー | `id` serial（サロゲートキー） | `(news_article_id, keyword_id)` 複合PK。サロゲートキー廃止 |
| UNIQUE制約 | `UNIQUE(news_article_id, keyword_id)` | 複合PK に吸収 |

### サロゲートキー廃止の理由

WatchlistEntry と同じ論理。`(news_article_id, keyword_id)` の複合キーで各行が一意に識別できる。このテーブルを FK で参照するテーブルは存在しない。

## 2. 属性の不変条件

### news_article_id（複合PK の一部）

| 項目 | 定義 |
|------|------|
| 型 | Integer |
| DB制約 | `NOT NULL`, `FOREIGN KEY REFERENCES news_articles(id) ON DELETE CASCADE`, 複合PK の一部 |
| 不変条件 | タグ付け対象の記事。変更不可 |
| 備考 | CASCADE により記事削除時にタグ付けも自動削除 |

### keyword_id（複合PK の一部）

| 項目 | 定義 |
|------|------|
| 型 | Integer |
| DB制約 | `NOT NULL`, `FOREIGN KEY REFERENCES keywords(id) ON DELETE CASCADE`, 複合PK の一部 |
| 不変条件 | 付与されたキーワード。変更不可 |
| 備考 | CASCADE によりキーワード削除時にタグ付けも自動削除 |

## 3. エンティティレベルの不変条件

| 制約 | 実現レイヤー | 説明 |
|------|-------------|------|
| 同一記事に同一キーワードの重複付与不可 | DB層（複合PK） | `(news_article_id, keyword_id)` の一意性 |
| タグ付けの不変性 | 設計原則 | 作成後に変更されない。削除のみ可能 |
| 記事削除時のクリーンアップ | DB層（FK CASCADE） | news_articles 削除で自動削除 |
| キーワード削除時のクリーンアップ | DB層（FK CASCADE） | keywords 削除で自動削除 |

## 4. 多層防御サマリ

| レイヤー | 防御内容 |
|----------|---------|
| **DB層** | 複合PK（重複防止）、FK CASCADE（両方向）、NOT NULL |

## 5. 設計判断の記録

| 判断 | 結論 | 理由 |
|------|------|------|
| サロゲートキー廃止 | 複合PK `(news_article_id, keyword_id)` に変更 | 複合キーで一意に特定可能。FK 参照元がない。WatchlistEntry と同じ論理 |
| created_at なし | 持たない | タグ付けの日時を記録する必要がない |
| テーブル名変更 | `news_keywords` → `article_keywords` | ドメインモデル（NewsArticle ↔ Keyword）と一致 |
