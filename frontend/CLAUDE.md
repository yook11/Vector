# frontend/ — Next.js フロントエンド

Next.js 16 (App Router) + TypeScript + Tailwind CSS + shadcn/ui + Biome によるダッシュボードUI。

## リサーチ義務

ライブラリの API に確信が持てない場合、推測でコードを書かず `/research` スキルを使うこと。
信頼できる情報源は `/research` スキルに定義済み。

## 型管理パイプライン

- **SSoT は `backend/app/schemas/` の Pydantic モデル**
- 型の流れ:
  1. FastAPI が `/openapi.json` を自動生成
  2. `/gen-types` スキルで `src/types/generated.ts` を自動生成
  3. `src/types/index.ts` で re-export + narrowing
- **`generated.ts` は手動編集禁止**

## コーディングルール

### 全般
- Biome (lint + format) に従う
- 型は `src/types/index.ts` 経由で利用（自動生成元: `/openapi.json`）

### コンポーネント設計
- Server Components をデフォルトとし、インタラクションが必要な場合のみ `"use client"`
- コンポーネントファイル名は PascalCase (例: `NewsCard.tsx`)

### 認証
- Better Auth を使用 — 実装時は `/better-auth` スキルを参照

### 状態管理
- Server Components + URL searchParams で管理
- グローバル状態管理ライブラリは当面導入しない

### API通信
- API 関数は `features/<name>/api/<verb-noun>.ts` に配置 (1 関数 1 ファイル)
- features の API 関数は `lib/api/server-fetcher.ts` または `lib/api/client-fetcher.ts` を経由
- 利用側は Public API (`@/features/<name>`) からのみ import (deep path 禁止)
- モック → 本番の切り替えは `NEXT_PUBLIC_API_URL` のみで完結させる

### ディレクトリ規約 (Bulletproof React 寄り)

```
src/
├── app/                  # routing & layout (Next.js 16 App Router)
├── features/             # backend ドメインに揃えた機能境界
│   ├── news/             # 記事閲覧 (collection)
│   ├── watchlist/        # ウォッチ (analysis)
│   ├── digest/           # 週次トレンド (digest)
│   ├── sources/          # ソース管理 (admin)
│   └── auth/             # 認証 UI
│       ├── components/
│       ├── api/
│       └── index.ts      # Public API (この feature の唯一の入口)
├── components/
│   ├── ui/               # shadcn/ui (不可侵)
│   ├── layout/           # Header / Sidebar / MobileNav 等
│   └── feedback/         # NotFoundMessage 等の横断 feedback
├── hooks/                # 横断 hooks (必要時のみ)
├── lib/
│   ├── api/              # server-fetcher / client-fetcher / fetcher / error / internal-config
│   ├── auth/             # auth / auth-client / session
│   ├── utils/            # cn / sanitize-url
│   ├── search-params/    # server / client
│   └── date.ts
├── types/                # 自動生成型 (generated.ts は手動編集禁止)
└── proxy.ts              # Next.js 16 公式リネーム (旧 middleware.ts)
```

**3 つのルール**:
1. features 同士の直接 import は禁止 (Biome `noRestrictedImports` で構造的に強制)
2. features を外から使う側は必ず Public API (`@/features/<name>`) を経由
3. features 名は backend ドメインに揃える (UI 露出のない `ingestion` は frontend に作らない)

例外: `features/news` から `features/watchlist` への一方向参照のみ許可 (NewsList が `WatchlistButton` を compose する役割のため。逆方向は不可)。

### スタイリング
- Tailwind CSS のユーティリティクラスを使用
- レスポンシブ対応: モバイルファーストで設計

## 禁止事項（NEVER）

1. **NEVER** 公式ドキュメントを確認せずに不確実なAPIの使い方を推測で書いてはならない → `/research` スキルを使うこと
2. **NEVER** Next.js の Pages Router パターン（`getServerSideProps`, `getStaticProps`, `pages/` ディレクトリ）を使ってはならない → App Router を使うこと
3. **NEVER** `any` 型を使用してはならない → 型は `src/types/index.ts` から導入
4. **NEVER** `components/ui/` 配下を手動編集してはならない → shadcn/ui の自動生成領域
5. **NEVER** `lib/api/server-fetcher.ts` / `client-fetcher.ts` を経由せずに backend を直接 fetch してはならない
6. **NEVER** Server Component で実現できる処理に `"use client"` を付けてはならない
7. **NEVER** カスタムCSSファイルを作成してはならない → Tailwind ユーティリティで解決すること
8. **NEVER** `useEffect` でデータフェッチしてはならない → Server Components または Route Handlers を使うこと
9. **NEVER** API レスポンスの型を手動定義してはならない → `/gen-types` スキルで自動生成された型を使うこと
10. **NEVER** `src/types/generated.ts` を手動編集してはならない → 自動生成ファイル

## 検証

タスク完了前に `/review` スキルを実行すること。

手動実行する場合:
```bash
npx biome check src/
npx tsc --noEmit
```