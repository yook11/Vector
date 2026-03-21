# Watchlist Entries テーブル設計（セキュア・バイ・デザイン）

> 作成日: 2026-03-22
> ソース: `specs/db-domain-model.md` セクション 2.13 WatchlistEntry

## 1. 概要

ユーザーが気になった記事を保存する機能。後で読み返すために使う。
User と NewsArticle の間の関連エンティティ。作成後に変更されない不変のエンティティ。

### 現行 → 新設計の変更点

| 項目 | 現行 | 新設計 |
|------|------|--------|
| テーブル名 | `watchlists` | `watchlist_entries`（1行が1つの保存操作であることを明確化） |
| 主キー | `id` serial（サロゲートキー） | `(user_id, news_article_id)` 複合PK。サロゲートキー廃止 |
| `user_id` | VARCHAR(32), FK なし | VARCHAR(32), FK `auth.user(id)` CASCADE |
| UNIQUE制約 | `UNIQUE(user_id, news_article_id)` | 複合PK に吸収 |

### サロゲートキー廃止の理由

`(user_id, news_article_id)` の複合キーが既に UNIQUE で NOT NULL であり、この組み合わせで各行が一意に識別できる。`id` が必要になるのは他のテーブルが FK で参照する場合だが、WatchlistEntry を参照するテーブルは存在しないし、ドメイン的にも「ウォッチリストのエントリを参照する何か」は想像しにくい。API のエンドポイントも `DELETE /watchlist/{article_id}`（user_id はヘッダーから取得）で自然に設計できる。

### auth スキーマ跨ぎ FK について

`user_id` は `auth.user(id)` への FK を張る。PostgreSQL はスキーマ跨ぎの FK をネイティブにサポートしている。必要な条件はマイグレーション順序の管理のみ:

1. Better Auth CLI でマイグレーション実行（`auth.user` テーブル作成）
2. その後に Alembic 実行（FK を含む `watchlist_entries` 作成）

この順序は Better Auth 移行計画（Phase 1）で既に確立されている。FK を張ることで、ユーザー削除時のデータ残留を防ぎ、多層防御の原則に沿う。

## 2. 属性の不変条件

### user_id（複合PK の一部）

| 項目 | 定義 |
|------|------|
| 型 | VARCHAR(32) |
| DB制約 | `NOT NULL`, `FOREIGN KEY REFERENCES auth.user(id) ON DELETE CASCADE`, 複合PK の一部 |
| 不変条件 | 保存したユーザー。変更不可 |
| 備考 | Better Auth の cuid 文字列。CASCADE によりユーザー削除時にエントリも自動削除。FK がない場合、削除されたユーザーの閲覧興味がDBに残存するリスクがある |

### news_article_id（複合PK の一部）

| 項目 | 定義 |
|------|------|
| 型 | Integer |
| DB制約 | `NOT NULL`, `FOREIGN KEY REFERENCES news_articles(id) ON DELETE CASCADE`, 複合PK の一部 |
| 不変条件 | 保存された記事。変更不可 |
| 備考 | CASCADE により記事削除時にエントリも自動削除。記事の削除自体は稀なケース（誤取り込みの除去、法的理由等） |

### created_at

| 項目 | 定義 |
|------|------|
| 型 | TIMESTAMP WITH TIME ZONE |
| DB制約 | `NOT NULL`, `DEFAULT NOW()` |
| 不変条件 | 保存日時。変更不可 |
| 備考 | ドメインモデルでは `savedAt` だが、他テーブルとの一貫性で `created_at` を維持。レコード作成 = 保存操作なので意味は同じ |

## 3. エンティティレベルの不変条件

| 制約 | 実現レイヤー | 説明 |
|------|-------------|------|
| 同一ユーザーが同一記事を重複保存不可 | DB層（複合PK） | `(user_id, news_article_id)` の一意性 |
| エントリの不変性 | 設計原則 | 作成後に変更されない。削除のみ可能 |
| ユーザー削除時のクリーンアップ | DB層（FK CASCADE） | `auth.user` 削除で自動削除。データ残留を防止 |
| 記事削除時のクリーンアップ | DB層（FK CASCADE） | `news_articles` 削除で自動削除。孤立レコードを防止 |
| 認証済みユーザーのみ操作可能 | アプリ層（BFF認証） | BFF がセッション検証後に X-User-ID をセット。未認証ユーザーは操作不可 |

## 4. 多層防御サマリ

| レイヤー | 防御内容 |
|----------|---------|
| **DB層** | 複合PK（重複防止）、FK CASCADE（user_id → auth.user、news_article_id → news_articles）、NOT NULL |
| **アプリ層** | BFF によるセッション検証、X-User-ID ヘッダーはサーバーサイドでセット |
| **ネットワーク層** | FastAPI は Docker internal ネットワークのみ。BFF 経由でのみアクセス可能。X-Internal-Secret による追加検証 |

## 5. 設計判断の記録

| 判断 | 結論 | 理由 |
|------|------|------|
| サロゲートキー廃止 | 複合PK `(user_id, news_article_id)` に変更 | 複合キーで一意に特定可能。FK 参照元がない。API も `DELETE /watchlist/{article_id}` + ヘッダー user_id で自然 |
| auth スキーマ跨ぎ FK | 張る | 多層防御の原則。FK がないとユーザー削除時にデータ残留リスク。マイグレーション順序は既に確立済み |
| ON DELETE CASCADE（両方） | user 削除でも記事削除でもエントリを自動削除 | ウォッチリストエントリは user と article の両方に依存する。どちらかが消えたらエントリも存在意義を失う |
| created_at の命名 | savedAt ではなく created_at を維持 | 他テーブルとの一貫性。レコード作成 = 保存操作なので意味は同じ |
