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
- features の API 関数は `lib/api/typed-server-fetcher.ts` (`typedServer` / `typedPublic`) を経由 (Server Component / Server Action 専用)
- mutation はすべて Server Action 化済み。Client Component からの直接 fetch は不要
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
│   ├── api/              # typed-server-fetcher / error / internal-config
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
   - ただし以下の意図的拡張は維持する (CLI 再生成時に逸脱として検出する目印):
     - `button.tsx`: `icon-xs` / `icon-sm` / `icon-lg` の size variant 拡張 (shadcn 標準は `default` / `sm` / `lg` / `icon`)
     - `alert-dialog.tsx`: `size?: "default" | "sm"` prop と全要素 `data-slot` 属性 (shadcn registry には未含まれる)
5. **NEVER** `lib/api/typed-server-fetcher.ts` を経由せずに backend を直接 fetch してはならない (Client Component から直接叩かない、mutation は Server Action 化する)
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
npm test
```

## テスト

### 実行コマンド
- `npm test` — 1 回実行 (CI と同じ)
- `npm run test:watch` — 開発中の watch
- `npm run test:coverage` — カバレッジ付き (CI で threshold check)
- `npm run test:e2e` — Playwright E2E (ローカル only、backend + dev server 起動済み前提)
- `npm run test:e2e:install` — 初回のみ chromium をインストール

### 配置規約
- co-locate: 対象ファイルと同階層に `<name>.test.ts` を置く
  例: `src/lib/utils/sanitize-url.ts` ↔ `src/lib/utils/sanitize-url.test.ts`
- vitest glob: `src/**/*.{test,spec}.{ts,tsx}` (e2e/ は coverage exclude)
- E2E: `frontend/e2e/<name>.spec.ts` に co-locate せず置く (Playwright が `testDir` で拾う)

### Phase 別スコープ
- **Phase 1**: 純関数 (security-critical な判定ロジック中心) — merged
- **Phase 2**: Server Action core 抽出 + 5 component の RTL — merged
- **Phase 3 (現状)**: E2E (Playwright) + msw 局所投入 + CI required + coverage threshold

### Mock 戦略
- `vi.mock("../api/<name>")` で **相対 path** で Server Action を mock (own feature 内のみ)
- `vi.mock("@/lib/auth/auth-client", ...)` で Better Auth client を置換
- `next/navigation` は `src/test/router-mock.ts` の helper で再利用
- **msw**: HTTP 層を network mock するときのみ。`server.use(http.<method>(...))` は test ファイル内で都度定義 (グローバル handler は持たない)。lifecycle は `vitest.setup.ts` で `beforeAll(listen)` / `afterEach(resetHandlers)` / `afterAll(close)`、未 handler の request は `bypass` で透過

### E2E (Playwright) の運用
- `frontend/playwright.config.ts` で project を 4 段に分離: `setup` / `anon` / `user` / `admin`
- `e2e/auth.setup.ts` が Better Auth `/api/auth/sign-in/email` に POST して storageState を `e2e/.auth/{user,admin}.json` に保存
- 認証後 spec は `storageState` を再利用、login/register spec のみ anon で UI 経由を維持
- backend (docker compose up) + dev server (npm run dev) を事前に起動した状態で `npm run test:e2e`
- Phase 3 では CI に乗せない (flaky 検証期間)。Phase 4 で smoke のみ昇格を再検討

### 禁止事項
- **NEVER** test 内で実 DB / 実 API を叩いてはならない (E2E はローカル backend のみ)
- **NEVER** `vi.mock` で features 横断 module を mock してはならない (テスト対象を絞る)
- **NEVER** msw handler を `src/test/msw/handlers/` 等で features 横断に集約してはならない (各 test 内で `server.use` する)
- **NEVER** test ファイルで `any` 型を使用してはならない (`as unknown as Foo` で対応)
- **NEVER** E2E spec の storageState を git に commit してはならない (`.gitignore` で構造的に防止)