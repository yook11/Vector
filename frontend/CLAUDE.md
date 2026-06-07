# frontend/ — Next.js フロントエンド

Next.js 16 (App Router) + TypeScript + Tailwind CSS + shadcn/ui + Biome によるダッシュボードUI。

## 型管理パイプライン

- **SSoT は `backend/app/schemas/` の Pydantic モデル**
- 型の流れ:
  1. FastAPI が `/openapi.json` を自動生成
  2. `/gen-types` スキル (= `npm run generate-types`) で `@hey-api/openapi-ts` 経由で `src/types/{types,sdk,client}.gen.ts` + `client/` + `core/` を自動生成
  3. `src/types/index.ts` で narrowing / discriminated union 再構築 / alias rename を集約 (手書き)
- **`*.gen.ts` / `client/` / `core/` は手動編集禁止** (自動生成領域)

## コンポーネント設計

- Server Components をデフォルトとし、インタラクションが必要な場合のみ `"use client"`
- コンポーネントファイル名は PascalCase (例: `NewsCard.tsx`)

## Task Agents

- frontend UI / component / page 実装は、利用可能な場合 frontend-ui-builder agent に分担する。

## 認証

Better Auth を使用 — 実装時は `/better-auth` スキルを参照。

## 状態管理

Server Components + URL searchParams で管理。グローバル状態管理ライブラリは当面導入しない。

## API通信

- API 関数は `features/<name>/api/<verb-noun>.ts` に配置 (1 関数 1 ファイル)
- backend 呼び出しは `@/types/sdk.gen` の hey-api SDK 関数を直接 call し、`lib/api/hey-api-interceptors.ts` 経由で 2 つの client を使い分ける (Server Component / Server Action 専用):
  - `client` (singleton): auth + error interceptor 付き。auth-required endpoint で per-call client なしに使う
  - `publicClient`: error interceptor のみ。`"use cache"` 内 anon endpoint で `{ client: publicClient }` を per-call 渡す (cookies/headers 読取を踏まないため)
- mutation はすべて Server Action 化済み。Client Component からの直接 fetch は不要
- 利用側は Public API (`@/features/<name>`) からのみ import (deep path 禁止)
- モック → 本番の切り替えは `NEXT_PUBLIC_API_URL` のみで完結させる

## features 構造ルール

backend ドメインに揃えた機能境界 (`auth` / `news` / `watchlist` / `digest` / `briefing` / `sources`)。

1. features 同士の直接 import は禁止 (Biome `noRestrictedImports` で構造的に強制)
2. features を外から使う側は必ず Public API (`@/features/<name>`) を経由
3. features 名は backend ドメインに揃える (UI 露出のない `ingestion` は frontend に作らない)

**例外**: `features/news` から `features/watchlist` への一方向参照のみ許可 (NewsList が `WatchlistButton` を compose する役割のため。逆方向は不可)。

## 禁止事項（NEVER）

1. **NEVER** 公式ドキュメントを確認せずに不確実なAPIの使い方を推測で書いてはならない → `/research` スキルを使うこと
2. **NEVER** Next.js の Pages Router パターン（`getServerSideProps`, `getStaticProps`, `pages/` ディレクトリ）を使ってはならない → App Router を使うこと
3. **NEVER** `any` 型を使用してはならない → 型は `src/types/index.ts` から導入
4. **NEVER** `components/ui/` 配下と `src/types/*.gen.ts` / `src/types/client/` / `src/types/core/` を手動編集してはならない (自動生成領域)
5. **NEVER** `lib/api/hey-api-interceptors.ts` の `client` / `publicClient` を経由せずに backend を直接 fetch してはならない (Client Component から直接叩かない、mutation は Server Action 化する)
6. **NEVER** Server Component で実現できる処理に `"use client"` を付けてはならない
7. **NEVER** カスタムCSSファイルを作成してはならない → Tailwind ユーティリティで解決すること
8. **NEVER** `useEffect` でデータフェッチしてはならない → Server Components または Route Handlers を使うこと

## 検証

実装変更後は `/check` スキルで、このディレクトリに該当する検証を実行する。

## テスト

詳細は `src/test/` 配下の指示を参照。
