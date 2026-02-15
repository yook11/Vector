# shared/ — APIスキーマ & 共有型定義

## 概要

フロントエンドとバックエンドの**契約（API Contract）**を管理するディレクトリ。
`openapi.yaml` が Single Source of Truth。

## ディレクトリ構成

```
shared/
└── api-schema/
    ├── openapi.yaml    # OpenAPI 3.1 仕様 (手動管理)
    └── types.ts        # openapi.yaml から生成される TypeScript 型
```

## ルール

### openapi.yaml
- 全エンドポイントの定義をここに集約する
- バックエンドの Pydantic スキーマと常に一致させること
- 命名規約: JSON フィールドは **camelCase**
- 変更時は影響範囲（フロント・バック双方）を確認

### types.ts
- openapi.yaml から自動生成する（手動編集禁止）
- フロントエンドは `frontend/src/types/index.ts` 経由で再エクスポートして利用

### 命名規約の対応表

| レイヤー | 規約 | 例 |
|---------|------|-----|
| DB (SQLModel) | snake_case | `news_article_id` |
| API (JSON) | camelCase | `newsArticleId` |
| TypeScript | camelCase | `newsArticleId` |

## 参照ドキュメント

- `docs/04_API_SPECIFICATION.md` — 全エンドポイント仕様
- `docs/02_DATABASE_DESIGN.md` — DB設計（スキーマ導出元）
