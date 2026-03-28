# ADR-002: PostgreSQL スキーマ分離（auth / public）

> 日付: 2026-03 / ステータス: Accepted

## Context

Better Auth 移行に伴い、認証テーブル（user, session, account, verification）と
アプリケーションテーブルの管理ツールが競合する問題が発生。

- Better Auth CLI (`npx @better-auth/cli migrate`) が認証テーブルを自動管理
- Alembic がアプリケーションテーブルを管理

両者が同一スキーマで動くとマイグレーション履歴が衝突する。

## Alternatives

| 案 | 評価 |
|---|---|
| 単一 `public` スキーマ | マイグレーションツール競合。不採用 |
| 別 DB インスタンス | 運用コスト過大。不採用 |
| **auth + public スキーマ分離** | 同一 DB で論理分離。**採用** |

## Decision

- `auth` スキーマ: Better Auth CLI が管理。Alembic の autogenerate から除外
- `public` スキーマ: Alembic が管理。アプリケーションテーブルのみ

## Rationale

- **ツール独立**: Better Auth と Alembic が互いを意識せず動作できる
- **関心の分離**: 認証はインフラ、ビジネスロジックは別レイヤー
- **将来性**: 認証を外部サービス（Auth0 等）に移行してもアプリスキーマは無影響
- **PostgreSQL ネイティブ**: スキーマ分離は PG の標準機能であり、特殊なワークアラウンド不要

## Consequences

- DB 接続時に `search_path TO auth, public` の設定が必要
- `watchlist_entries.user_id` → `auth.user.id` の FK はスキーマ横断
- Alembic `env.py` で `include_name` フィルタにより auth スキーマを除外
