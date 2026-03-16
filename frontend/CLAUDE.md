# frontend/ — Next.js フロントエンド

Next.js 16 (App Router) + TypeScript + Tailwind CSS + shadcn/ui + Biome によるダッシュボードUI。

## 公式ドキュメント参照先（リサーチ義務）

実装においてAPI仕様に確信が持てない場合、推測でコードを書かず、
必ず以下のURLを `WebFetch` して一次情報を確認すること。

| ライブラリ | 公式URL（ここからFetchすること） | 注意点・制約 |
|---|---|---|
| Next.js 16+ | `https://nextjs.org/docs/app` | **Pages Routerは絶対に使用禁止。必ず `/app` 配下を参照すること** |
| React | `https://react.dev/` | 旧サイト（reactjs.org）は参照しないこと |
| shadcn/ui | `https://ui.shadcn.com/docs` | コンポーネントの追加・実装方法の確認用 |
| Tailwind CSS | `https://tailwindcss.com/docs` | |
| TypeScript | `https://www.typescriptlang.org/docs/` | |
| NextAuth.js | `https://next-auth.js.org/` | **App Router 向け設定に注意。v4 を使用** |
| openapi-typescript | `https://openapi-ts.dev/` | 型生成パイプライン (`npm run generate-types`) の設定確認用 |

## 型管理パイプライン

- **SSoT は `backend/app/schemas/` の Pydantic モデル**
- 型の流れ:
  1. FastAPI が `/openapi.json` を自動生成
  2. `npm run generate-types` で `src/types/generated.ts` を自動生成
  3. `src/types/index.ts` で re-export + narrowing
- **`generated.ts` は手動編集禁止**

## コーディングルール

### 全般
- Biome (lint + format) に従う
- 型は `src/types/index.ts` 経由で利用（自動生成元: `/openapi.json`）

### コンポーネント設計
- Server Components をデフォルトとし、インタラクションが必要な場合のみ `"use client"`
- コンポーネントファイル名は PascalCase (例: `NewsCard.tsx`)

### 状態管理
- Phase 1: Server Components + URL searchParams で管理
- グローバル状態管理ライブラリは Phase 1 では導入しない

### API通信
- `lib/api-client.ts` を唯一のAPI通信レイヤーとする
- モック → 本番の切り替えは `NEXT_PUBLIC_API_URL` のみで完結させる

### スタイリング
- Tailwind CSS のユーティリティクラスを使用
- レスポンシブ対応: モバイルファーストで設計

## 禁止事項（NEVER）

1. **NEVER** 公式ドキュメントを確認せずに不確実なAPIの使い方を推測で書いてはならない
2. **NEVER** Next.js の Pages Router パターン（`getServerSideProps`, `getStaticProps`, `pages/` ディレクトリ）を使ってはならない → App Router を使うこと
3. **NEVER** `any` 型を使用してはならない → 型は `src/types/index.ts` から導入
4. **NEVER** `components/ui/` 配下を手動編集してはならない → shadcn/ui の自動生成領域
5. **NEVER** `lib/api-client.ts` を経由せずにAPIを直接呼び出してはならない
6. **NEVER** Server Component で実現できる処理に `"use client"` を付けてはならない
7. **NEVER** カスタムCSSファイルを作成してはならない → Tailwind ユーティリティで解決すること
8. **NEVER** `useEffect` でデータフェッチしてはならない → Server Components または Route Handlers を使うこと
9. **NEVER** API レスポンスの型を手動定義してはならない → `npm run generate-types` で自動生成された型を使うこと
10. **NEVER** `src/types/generated.ts` を手動編集してはならない → 自動生成ファイル

## 検証コマンド

```bash
# タスク完了前に必ず実行
npx biome check src/
npx tsc --noEmit
```

## 参照ドキュメント

- `docs/04_API_SPECIFICATION.md` — API仕様詳細