# frontend/ — Next.js フロントエンド

## 概要

Next.js 14 (App Router) + TypeScript + Tailwind CSS + shadcn/ui によるダッシュボードUI。

## 技術スタック

- Next.js 14+ (App Router)
- TypeScript (strict mode)
- Tailwind CSS
- shadcn/ui (コンポーネントライブラリ)

## ディレクトリ構成

```
frontend/
├── Dockerfile
├── package.json
├── tsconfig.json
├── tailwind.config.ts
├── next.config.js
├── components.json          # shadcn/ui 設定
└── src/
    ├── app/
    │   ├── layout.tsx
    │   ├── page.tsx           # ダッシュボード
    │   ├── settings/page.tsx  # キーワード設定
    │   ├── news/[id]/page.tsx # ニュース詳細
    │   └── api/mock/          # モックAPI (Route Handlers)
    ├── components/
    │   ├── layout/            # Header, Sidebar, Footer
    │   ├── news/              # NewsCard, NewsList, NewsDetail, SentimentBadge
    │   ├── keywords/          # KeywordManager, KeywordTag
    │   └── ui/                # shadcn/ui (自動生成、手動編集禁止)
    ├── lib/
    │   ├── api-client.ts      # API呼び出し (型安全)
    │   └── utils.ts
    ├── hooks/
    │   ├── useNews.ts
    │   └── useKeywords.ts
    └── types/
        └── index.ts           # shared/api-schema/types.ts を再エクスポート
```

## コーディングルール

### 全般
- コード中のコメント・変数名は**英語**
- ESLint + Prettier に従う
- `any` 型の使用禁止。型は `shared/api-schema/types.ts` から導入

### コンポーネント設計
- Server Components をデフォルトとし、インタラクションが必要な場合のみ `"use client"`
- shadcn/ui の `ui/` ディレクトリは自動生成のため手動編集しない
- コンポーネントファイル名は PascalCase (例: `NewsCard.tsx`)

### 状態管理
- Phase 1: Server Components + URL searchParams で管理
- グローバル状態管理ライブラリは Phase 1 では導入しない

### API通信
- `lib/api-client.ts` を唯一のAPI通信レイヤーとする
- 環境変数で接続先を切り替え:
  ```typescript
  const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "/api/mock";
  ```
- モック → 本番の切り替えは `NEXT_PUBLIC_API_URL` のみで完結させる

### スタイリング
- Tailwind CSS のユーティリティクラスを使用
- カスタムCSSは原則不要。必要な場合は Tailwind の `@apply` で
- レスポンシブ対応: モバイルファーストで設計

## 参照ドキュメント

- `shared/api-schema/openapi.yaml` — APIスキーマ (Single Source of Truth)
- `docs/04_API_SPECIFICATION.md` — API仕様詳細
