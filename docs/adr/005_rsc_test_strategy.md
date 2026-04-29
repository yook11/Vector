# ADR-005: RSC (Server Component) ユニットテスト戦略

> 日付: 2026-04 / ステータス: Proposed

## Context

Phase 3 で frontend に Playwright E2E + msw + Vitest 統合を導入し、PR #257 で smoke (anon: login + register、4 ケース) が CI matrix に昇格した。これにより認証経路 (ADR-002 / ADR-003) を含む user flow の regression 検出基盤は揃った。

しかし現状の test pyramid を実装観点で精査すると **RSC (Server Component) 専用の検証経路が欠落している** ことが分かる。

| レイヤ | 経路 | 状態 |
|---|---|---|
| Client Component | Vitest + jsdom + RTL | 5 ファイル / coverage 99.14% (Phase 2 PR #246) |
| 純ロジック / Server Action core | Vitest 経路 (`*-cores.ts` 抽出) | PR #246 で pattern 確立、coverage include 明示 |
| RSC (Server Component) | **経路なし** | E2E (Playwright) で間接的にカバーされるのみ |

注: 現状 `vitest.config.mts` 全体が `environment: "jsdom"` で、既存の `*-cores.test.ts` も jsdom 上で動いている。純ロジックなので jsdom でも動作上の問題はないが、本 ADR で RSC project (node) を新設する際、**既存 cores を node 側に移すことは scope 外** とする (Consequences の PR-A 説明を参照)。

RSC 側のロジックは決して薄くない。実装を確認した範囲で例示すると:

- `app/(protected)/weekly-trends/page.tsx` — `data.state === "empty"` の 2 値分岐、`weekStart`/`weekEnd` の date format、`sourceAnalysisCount` の文字列化
- `app/(protected)/news/[id]/page.tsx` — `notFound()` への分岐、`ApiError.status === 404` 判定、**Promise.all を使わず分割 await して「watchlist 取得失敗を 404 と誤認しない」非自明ロジック**、Suspense 内 async child の error 握り潰し

これらを E2E 経由でしか検証できない状態は次の問題を抱える:

1. **遅い** — Playwright で 1 ケース 1〜2s。RSC 分岐網羅を E2E に積むと PR #257 の 2m31s が線形に膨らむ
2. **不安定 (flaky)** — seed データ状態に依存。fresh DB で empty 状態を再現するには fixture 追加が必要
3. **網羅困難** — data shaping ロジック (例: `formatDate(weekStart)–formatDate(weekEnd)` の境界) は E2E の検証点が遠すぎる
4. **責務違反** — E2E smoke は user flow regression 検出のために anon スコープへ意図的に絞っている (PR #257 の判断)。ロジック網羅は E2E の責務外

本 ADR は RSC 専用の検証経路を確立し、test pyramid を完成させる。

## Alternatives

| 案 | 概要 | 評価 |
|---|---|---|
| A: Vitest projects 分離 | `client (jsdom)` / `rsc (node)` の 2 project を `test.projects` で分離。RSC は **ロジックを `page-models/` に抽出** して node 経路で厚くテスト | **採用** |
| B: E2E (Playwright) 拡張 | 現状の延長。RSC ロジックは E2E spec で間接的にカバー | 不採用 |
| C: pragma `// @vitest-environment node` ファイル単位切替 | `vitest.config` 変更なし、ファイル冒頭で environment 切替 | 不採用 |

### 案 B 不採用の根拠

- 1 ケース 1〜2s で **CI 時間が線形に膨らむ** (PR #257 anon 4 ケースで既に 2m31s)
- seed 状態依存で **flaky 化リスク** が高い (PR #255 で既に selector drift と route-announcer の罠で fix 必要だった経緯あり)
- **ロジック単位の網羅は事実上不可能**: data shape 整形 (例: `weekLabel` 文字列の境界条件) は E2E の検証点が遠すぎる

### 案 C 不採用の根拠

- pragma が散在すると **書き忘れで jsdom に落ちて謎エラー** ("self is not defined" 等)
- project 単位の設定 (setupFiles / msw server / alias) を **共有できない** ため duplicate が発生
- 試験導入用途では便利だが、ファイル数増加で結局 A に回帰する。**移行コストを後回しにしただけ**

### 補足: なぜ RTL で RSC を直接 render しないか

`@testing-library/react@16.3.2` (本 repo の採用版) は React 19 互換だが、**RSC 直接 render は未サポート**。関連 issue:

- testing-library/react-testing-library#1209
- testing-library/react-testing-library#1375

また `renderToString` は **Suspense を待機しない** ことが React 公式で明記されている (`renderToString` は Suspense fallback を吐くだけ。data 待機が必要なら `renderToPipeableStream` などの streaming 系を使う)。
https://react.dev/reference/react-dom/server/renderToString

→ RSC 全体を文字列ダンプして assertion する方針は信頼性が低い。**ロジックを model 関数として抽出し、その関数を node project で test する** のが現実解。

## Decision

**Vitest projects 分離 + 既存 `*-cores.ts` pattern の RSC への拡張** を採用する。

### 1. Vitest projects 配置

`vitest.config.mts` を `test.projects` に書き換える (Vitest 3.2 で `defineWorkspace` が deprecated となり、`test.projects` が現在の推奨 API):

```ts
test: {
  projects: [
    {
      extends: true,
      test: {
        name: "client",
        environment: "jsdom",
        setupFiles: ["./vitest.setup.client.ts"],
        include: ["src/**/*.{test,spec}.{ts,tsx}"],
        exclude: ["src/**/*.node.{test,spec}.{ts,tsx}"],
      },
    },
    {
      extends: true,
      test: {
        name: "rsc",
        environment: "node",
        setupFiles: ["./vitest.setup.node.ts"],
        include: ["src/**/*.node.{test,spec}.{ts,tsx}"],
      },
    },
  ],
}
```

**ファイル名 suffix `.node.test.ts` で project を振り分ける**。pragma 散在を避け、ディレクトリ規則と独立に project 切替を可能にする。

### 2. RSC ロジック抽出規約

Phase 2 PR #246 で確立した `*-cores.ts` pattern を RSC page にも適用する:

- `features/<area>/api/*-cores.ts` (既存) — Server Action / fetcher の純ロジック
- `features/<area>/page-models/*.ts` (**新規**) — RSC page の data shaping / 認可判定 / 404 判定 / fallback 判定

`page.tsx` は model を呼んで JSX 化する **薄い層** に限定する:

```ts
// features/digest/page-models/weekly-trends.ts
export type WeeklyTrendsPageModel =
  | { state: "empty" }
  | {
      state: "ready";
      weekLabel: string;
      categories: WeeklyTrendsCategory[];
      sourceAnalysisCount: number;
    };

export async function getWeeklyTrendsPageModel(): Promise<WeeklyTrendsPageModel> {
  const data = await getWeeklyTrends();
  if (data.state === "empty") return { state: "empty" };
  return {
    state: "ready",
    weekLabel: `${formatDate(data.weekStart)} – ${formatDate(data.weekEnd)}`,
    categories: data.categories,
    sourceAnalysisCount: data.sourceAnalysisCount,
  };
}

// app/(protected)/weekly-trends/page.tsx
export default async function WeeklyTrendsPage() {
  const model = await getWeeklyTrendsPageModel();
  if (model.state === "empty") return <WeeklyTrendsEmpty />;
  return <WeeklyTrendsLayout model={model} />;
}
```

### 3. テスト配置

- **page-models**: rsc project で `getXxxPageModel()` の empty / ready / error / 404 を網羅
- **page.tsx 自体**: model 抽出後は薄い JSX 化のみなので **smoke 1〜2 ケース** に留める。具体的には:
  - 基本は **「model の state に応じて期待する component が選ばれているか」** を `await Page()` の戻り ReactElement に対して確認 (例: `element.type === WeeklyTrendsEmpty` / `WeeklyTrendsLayout`)
  - HTML 文字列 assertion は **主要文言の存在確認まで** に限定 (例: empty 状態で「次回の自動生成は」が含まれる)
  - 深い DOM tree assertion / 子コンポーネント props の網羅は **追わない** (それは page-models と Client Component test の責務)
- **Client Component**: 現状どおり client project で RTL
- **E2E**: smoke (login / register / 重要 user flow / hydration regression) に限定。**RSC ロジック網羅は E2E の責務外**

### 4. msw 経路

`src/test/msw/server.ts` の per-test `server.use(...)` 規約は両 project で共有する (各 setup file から同一 server を import)。`frontend/CLAUDE.md` の **「features 横断 module を mock してはならない」** 原則は維持し、横断 handler を作らない。

## Rationale

### なぜ案 A か

- **検出能力**: 数十 ms / ケースで分岐網羅可能。E2E より 1〜2 桁速く、seed 依存もない
- **保守コスト**: `test.projects` の設定 1 箇所で済む。pragma 散在 (案 C) や Playwright spec 増殖 (案 B) より明確
- **既存資産流用**: msw setup / `*-cores.ts` pattern / Phase 2 PR #246 の規約をそのまま延伸できる
- **Vitest 公式の推奨 API**: `defineWorkspace` は v3.2 で deprecated、`test.projects` が現在の推奨形式。Vector は既に `vitest@^4.1.5` を採用済で互換性問題なし
  - https://vitest.dev/guide/projects.html

### なぜ既存 `*-cores.ts` pattern を再利用するか

Phase 2 PR #246 で `watchlist-cores.ts` / `source-cores.ts` を導入し、Server Action の純ロジックを抽出して node 経路で test する規約を確立済 (両者は coverage include にも明示されている)。

本 ADR は **新パターンを増やすのではなく、同 pattern の適用範囲を RSC page に広げる** だけ。これにより:

- 認知負荷が増えない — 既存規約の延長として読める
- レビュー時の指摘軸が一貫する — "model 抽出が薄い"、"page.tsx に分岐残っている" 等
- coverage threshold (Phase 3 で導入: statements/lines/functions 90、branches 85) を page-models にも自然に拡張できる

### なぜ E2E (案 B) で代替しないか

PR #257 の smoke (anon: login + register、4 ケース) で **既に 2m31s**。RSC ロジック網羅を E2E に積むと CI 時間が線形に膨らみ、smoke 本来の責務 (user flow regression) を圧迫する。

E2E と unit test は責務が違う:

- E2E: user flow / hydration / 認証経路 / cross-page navigation の regression
- unit (RSC node project): data shaping / 認可判定 / error 分岐 / 404 / fallback

両者は **代替関係ではなく相補関係**。

## Consequences

### 必要な作業

本 ADR 採用後、別 PR で順次実装する:

1. **PR-A (土台)** — `vitest.config.mts` を `test.projects` 化、`vitest.setup.{client,node}.ts` を分離、msw server を両 project から import 可能に。既存 test の include path / file 名は変更しない (suffix `.node.test.ts` を新規追加分にのみ適用)。**既存の `*-cores.test.ts` は当面 client project (jsdom) のまま維持** — 純ロジックなので jsdom でも動作問題なし、PR-A の差分を最小化するため移行は本 ADR の scope 外とする
2. **PR-B (weekly-trends)** — `features/digest/page-models/weekly-trends.ts` 抽出 + node test、`page.tsx` を model 呼出しに薄化
3. **PR-C (news/[id])** — `features/news/page-models/news-detail.ts` 抽出 (Promise 分割 + 404 分岐 + watchlist failure 隔離ロジック)
4. **PR-D (dashboard / search)** — search query parsing は既に `lib/search-params/server.ts` 抽出済。残る fetch 結果整形を抽出
5. **PR-E (watchlist / source-admin)** — Suspense 内 async child が絡むため、丸ごと render より抽出関数 + cores 優先

### 優先順位の根拠

- **#1 weekly-trends** — 分岐が 2 値 (empty / ready) で導入コスト最小。**pattern 確立に最適**
- **#2 news/[id]** — 抽出価値が最も高い (Promise 分割 + 404 誤認回避) が、`notFound()` mock + Suspense mock のコストが大きい。pattern 確立後に着手
- **#3 dashboard** — `search-params/server.ts` が既存のため、追加抽出範囲が小さい
- **#4 watchlist / source-admin** — Suspense ネストが深く、抽出粒度の判断が必要。**最後**

### 制約 / 注意

- `next/cache` / `next/navigation` (notFound, redirect) / `next/headers` (cookies) の mock は **node project の setup file で明示**。pragma 散在 (案 C) 不採用の根拠でもある
- RSC test は **`renderToString` による HTML 文字列 assertion を主戦場にしない**。React 公式の Suspense 制限と RTL の RSC 未対応が理由 (Alternatives 補足参照)
- model 抽出時に「page.tsx に残すべき JSX 構造」と「model に押し出すべき分岐」の境界線は PR レビューで判断する。原則は **「副作用なしで純粋に test できる単位を model に出す」**
- 既存 E2E スコープ (PR #257 anon = login + register) は変更しない。data 依存 spec の CI 昇格は別 ADR / 別 PR で扱う (Phase 4 候補)

### 採用後の責任分担

- 新規 page.tsx は model 抽出 pattern に従う (PR レビュー軸として確立)
- coverage threshold (S/L/F 90 / B 85) を page-models ディレクトリにも適用
- `frontend/CLAUDE.md` に本 ADR への言及を追加 (PR-A 着手時)

## 参考

- ADR-002: auth schema 分離 (test 経路で auth.cli.ts duplicate を要求した制約の根拠)
- ADR-003: BFF プロキシ (RSC が触る fetcher 層の前提)
- Vitest projects: https://vitest.dev/guide/projects.html
- React `renderToString` の Suspense 制限: https://react.dev/reference/react-dom/server/renderToString
- @testing-library/react RSC support issues: #1209, #1375
- PR #246 (Phase 2 PR-3): `*-cores.ts` pattern の確立
- PR #257: E2E smoke の CI 昇格 (本 ADR の前提)
