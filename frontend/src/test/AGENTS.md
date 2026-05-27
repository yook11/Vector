# frontend/src/test/ — フロントエンドテストガイド

vitest (unit / component / RSC) と Playwright (E2E) の両方をここで扱う。

## 実行コマンド

- `npm test` — vitest 1 回実行 (CI と同じ)
- `npm run test:watch` — 開発中の watch
- `npm run test:coverage` — カバレッジ付き (CI で threshold check)
- `npm run test:e2e` — Playwright E2E (ローカル only、backend + dev server 起動済み前提)
- `npm run test:e2e:install` — 初回のみ chromium をインストール

## 配置規約

- **co-locate**: 対象ファイルと同階層に `<name>.test.ts` を置く
  例: `src/lib/utils/sanitize-url.ts` ↔ `src/lib/utils/sanitize-url.test.ts`
- **vitest glob**: `src/**/*.{test,spec}.{ts,tsx}` (e2e/ は coverage exclude)
- **E2E**: `frontend/e2e/<name>.spec.ts` (co-locate せず Playwright の `testDir` で拾う)

## Mock 戦略

- `vi.mock("../api/<name>")` で **相対 path** で Server Action を mock (own feature 内のみ)
- `vi.mock("@/lib/auth/auth-client", ...)` で Better Auth client を置換
- `next/navigation` は `src/test/router-mock.ts` の helper で再利用
- **msw**: HTTP 層を network mock するときのみ。`server.use(http.<method>(...))` は test ファイル内で都度定義 (グローバル handler は持たない)。lifecycle は `vitest.setup.ts` で `beforeAll(listen)` / `afterEach(resetHandlers)` / `afterAll(close)`、未 handler の request は `bypass` で透過

## E2E (Playwright)

- `frontend/playwright.config.ts` で project を 4 段に分離: `setup` / `anon` / `user` / `admin`
- `e2e/auth.setup.ts` が Better Auth `/api/auth/sign-in/email` に POST して storageState を `e2e/.auth/{user,admin}.json` に保存
- 認証後 spec は `storageState` を再利用、login/register spec のみ anon で UI 経由を維持
- backend (docker compose up) + dev server (npm run dev) を起動した状態で実行

## 禁止事項（NEVER）

- **NEVER** test 内で実 DB / 実 API を叩いてはならない (E2E はローカル backend のみ)
- **NEVER** `vi.mock` で features 横断 module を mock してはならない (テスト対象を絞る)
- **NEVER** msw handler を `src/test/msw/handlers/` 等で features 横断に集約してはならない (各 test 内で `server.use` する)
- **NEVER** test ファイルで `any` 型を使用してはならない (`as unknown as Foo` で対応)
- **NEVER** E2E spec の storageState を git に commit してはならない
