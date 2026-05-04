# @hey-api/openapi-ts 移行リサーチ結果

調査日: 2026-05-04
対象: Vector frontend の `openapi-typescript@7.x` + `openapi-fetch@0.17` から `@hey-api/openapi-ts` 系へ移行する Stage 2 プラン策定の根拠材料

---

## TL;DR (要点先出し)

- 安定版は **@hey-api/openapi-ts 0.97.1** (2026-04-28 リリース)。**0.x 継続**でメジャー安定版 (1.x) はまだ。production 採用 (Vercel/PayPal/OpenCode) はあるが SemVer 上は initial development phase。pin 必須。
- TypeScript peer: **`>=5.5.3 || >=6.0.0`** (TS 6.x 対応済)。Node: **>=22.13.0** 必須。
- `@hey-api/client-next` の `Config` は `extends Omit<RequestInit, 'body' | 'headers' | 'method'>` なので **`next: { tags, revalidate }` / `cache` は Native fetch options としてそのまま素通し**。これが移行の最大決定要因。
- **client は cookies()/headers() を内部で呼ばない**。bundle/client.ts 実装を読んだ限り `globalThis.fetch` を直接呼ぶだけ。Next.js 16 Cache Components (`"use cache"`) 内で問題なく呼べる。
- error 発生時は **生の JSON object または text 文字列を `throw`** する。専用 error class でラップしない。FastAPI の `{detail: ...}` shape は error interceptor で normalize 可能。
- **throwOnError 設定の TypeScript 型反映には実験的 parser でのバグ報告 (#1565) あり**。実質はランタイムでは動作するが、型レベルで `data: T | undefined` が残る場合あり。要 verify (移行 PoC で型確認)。
- 出力ファイル名は **`types.gen.ts` / `sdk.gen.ts` / `client.gen.ts` / `index.ts` で固定**。base ディレクトリのみ output で制御可能。`src/types/` 直下に並べる構成は問題ない (`output: 'src/types'`)。

---

## 1. @hey-api/openapi-ts のセットアップ

### 公式 doc URL
- https://heyapi.dev/openapi-ts/get-started
- https://heyapi.dev/openapi-ts/configuration
- https://github.com/hey-api/openapi-ts (packages/openapi-ts/package.json)

### 検証結果サマリ
- **最新安定版**: `@hey-api/openapi-ts@0.97.1` (2026-04-28)。直近メジャー履歴: 0.94.4 (2026-03-20) → 0.95.0 (2026-04-02, Node 22.13 へ) → 0.97.0 (interceptor / throwOnError 修正) → 0.97.1。
- **TypeScript peer**: `">=5.5.3 || >=6.0.0 || 6.0.1-rc"` (Vector frontend は TS 5.9 系のはずなので互換)。
- **Node**: `>=22.13.0` (Vector の Node 22 系で OK)。
- **plugin 構成**: 推奨 = `@hey-api/typescript` (型定義) + `@hey-api/sdk` (SDK 関数群、デフォルト on) + `@hey-api/client-next` (Next.js 用 fetch wrapper)。`@hey-api/typescript` と `@hey-api/sdk` は plugin array を省略するとデフォルトで自動有効化される。
- **設定ファイル**: `openapi-ts.config.ts` (jiti loader 経由、`.cjs/.mjs/.js` も可)。
- **インストール**: `npm add @hey-api/openapi-ts -D -E` (-E = exact pin、公式推奨)。

### コード例 (公式)

```ts
// openapi-ts.config.ts
import { defineConfig } from '@hey-api/openapi-ts';

export default defineConfig({
  input: 'http://localhost:8000/openapi.json', // or 'openapi.json' file path
  output: 'src/types', // ← Vector の現状 src/types/ に揃えられる
  plugins: [
    '@hey-api/typescript',
    '@hey-api/sdk',
    {
      name: '@hey-api/client-next',
      runtimeConfigPath: './src/lib/api/hey-api.config.ts',
    },
  ],
});
```

### output で `src/types/` 配下に出力できるか
- **Yes**。`output: 'src/types'` で `src/types/{client.gen.ts, sdk.gen.ts, types.gen.ts, index.ts, client/, core/}` が生成される。
- ただし**ファイル名は変更不可** (types.gen.ts など fixed)。`output: { entryFile: false, path: ... }` で `index.ts` の生成抑止のみ可能。
- 出力ディレクトリは「dependency として扱え」と公式が明記。手動編集禁止。

### 不明点
- なし (構造は完全把握)。

---

## 2. @hey-api/client-next の Next.js Cache Components 整合性 【最重要】

### 公式 doc URL
- https://heyapi.dev/openapi-ts/clients/next-js
- ソース: https://github.com/hey-api/openapi-ts/tree/main/packages/openapi-ts/src/plugins/@hey-api/client-next/bundle (`client.ts`, `types.ts`, `utils.ts`)

### 検証結果サマリ (実装ソース確認済み)

**(a) cookies/headers を内部参照しない**
- `bundle/client.ts` の `request` 実装は `_fetch(url, requestInit)` を直接呼ぶだけ。`requestInit` は `{...opts, body}` のスプレッド。
- `next/headers` や `next/cookies` を import している箇所は **package 全体で 0**。
- → **`"use cache"` 内で安全に呼べる** (Next.js 16 が禁じる runtime API access が無い)。

**(b) `setConfig` vs `createClient` vs `runtimeConfigPath`**
- 公式推奨 = **`runtimeConfigPath` + `createClientConfig()`** (Next.js のため明示的に推奨)。RSC/edge/client component 全環境で初期化保証。
- `setConfig({ baseUrl })` は「どこでも呼べるが、初回 client 利用前に呼ばれていないと miss する」リスクあり。
- `createClient()` は別 instance を作る用途 (multi-tenant や test)。
- 設定ファイルは `output` フォルダからの **相対パス解決** に v0.97.0 で変更されている (migration guide)。

```ts
// runtimeConfigPath: src/lib/api/hey-api.config.ts
import type { CreateClientConfig } from '../../types/client.gen';

export const createClientConfig: CreateClientConfig = (config) => ({
  ...config,
  baseUrl: process.env.INTERNAL_BACKEND_BASE_URL,
});
```

**(c) per-request state を持たないか**
- `createClient()` は closure で `_config` を持つ module-singleton。グローバル mutable state は `_config`、`interceptors.fns` の 2 つのみ。
- リクエスト処理は引数 `options` を local 変数 `opts = {..._config, ...options}` でマージしてから fetch を呼ぶ純関数的構造。**per-request state は無い**。
- → module singleton client で `setConfig({ baseUrl: ... })` を起動時に 1 回呼ぶ運用で OK。

**(d) `next: { tags: [...], revalidate: number }` の素通し**
- `bundle/types.ts` `Config` 型: `extends Omit<RequestInit, 'body' | 'headers' | 'method'>`
- → `next` / `cache` などの **Next.js 拡張 RequestInit プロパティはそのまま型に乗る**。
- ランタイム側 `bundle/client.ts` でも `requestInit = { ...opts, body: getValidRequestBody(opts) }` で `next` フィールドは破棄されず fetch にそのまま渡る。
- 既知 issue: openapi-fetch 側 (#1569) では options 抜き取り問題があったが、**hey-api/client-next は構造的に解決済み** (Spread 経路で素通し)。
- 関連: hey-api/openapi-ts#1515 で「Next.js client を作る」という TODO が出ており、その成果物が現在の `@hey-api/client-next`。

```ts
// 想定 Vector 利用例
import { getApiV1WatchlistMe } from '@/types/sdk.gen';

const { data } = await getApiV1WatchlistMe({
  next: { tags: [cacheTags.watchlistMe] },
  throwOnError: true,
});
```

**(e) フォールバック: `@hey-api/client-fetch`**
- 既知 issue 範囲では client-next で問題ないが、もし future version で next 依存が混入したら、`@hey-api/client-fetch` (より低レベル、generic Fetch wrapper) に plugin を 1 行差し替えで切替可能。生成 SDK 側 API は同一 (`Options<TData>` interface 統一)。
- → リスク隔離として acceptable。

### 不明点
- 公式 doc 自体は `next` options に明示的言及がない (実装からの確認となる)。official changelog / next-js client doc に追記される可能性あり。

---

## 3. interceptor / middleware API

### 公式 doc URL
- https://heyapi.dev/openapi-ts/clients/next-js (interceptors 節)
- ソース: `bundle/utils.ts` の `Interceptors` class

### 検証結果サマリ (実装ソース確認済み)

3 種類の interceptor : `request` / `response` / `error`。それぞれ `use(fn) → id` / `eject(id|fn)` / `update(id, fn)` / `clear()` を持つ。

**シグネチャ (utils.ts より verbatim)**
```ts
type ReqInterceptor<Options> = (options: Options) => void | Promise<void>;
type ResInterceptor<Res, Options> = (response: Res, options: Options) => Res | Promise<Res>;
type ErrInterceptor<Err, Res, Options> = (
  error: Err,
  response: Res | undefined, // network error 時は undefined
  options: Options,
) => Err | Promise<Err>;
```

**使用例**
```ts
import { client } from '@/types/client.gen';

// Authorization header を request 直前に注入
client.interceptors.request.use(async (options) => {
  options.headers.set('Authorization', `Bearer ${await getToken()}`);
  // ↑ Headers instance の mutate-in-place が可能
  // 戻り値は void。new Request を return する必要なし
});

// FastAPI 風 error を ApiError へ正規化
client.interceptors.error.use(async (error, response, options) => {
  if (error && typeof error === 'object' && 'detail' in error) {
    return new ApiError(response?.status ?? 0, normalizeErrorDetail((error as any).detail));
  }
  return new ApiError(response?.status ?? 0, String(error));
});
```

### 重要な挙動
- request interceptor は **options object を mutate** する形 (新しい Request を返さない)。`options.headers` は `Headers` instance。`set` / `append` / `delete` で操作。
- response interceptor は new Response を return する必要あり。
- error interceptor は新 error を return すると次の interceptor & 最終 throw に伝播 (chain)。**v0.97.0 で response/request と同じ chain pattern に揃った**。
- onRequest/onResponse/onError 名は **本 client では使わない** (interceptors API のみ)。openapi-fetch の Middleware.onRequest({ request }) からの命名差異あり、要意識。

### 不明点
- request interceptor は void だが Promise 内部で options を await mutate しても順序保証はある (sequential await loop 実装、確認済)。

---

## 4. throwOnError と error 正規化

### 公式 doc URL
- https://github.com/orgs/hey-api/discussions/740
- https://github.com/hey-api/openapi-ts/issues/1565
- https://github.com/hey-api/openapi-ts/issues/914
- ソース: `bundle/client.ts` の `request` 関数
- ソース: `bundle/types.ts` の `RequestResult` 型

### 検証結果サマリ (実装ソース確認済み)

**(a) throwOnError 時の throw 内容**
- 4xx/5xx 時、`bundle/client.ts` 内で:
  ```ts
  const textError = await response.text();
  let jsonError: unknown;
  try { jsonError = JSON.parse(textError); } catch {}
  throw jsonError ?? textError;
  ```
- → **生の JSON object または text 文字列が throw される**。`HeyApiError` のような独自 class は無い。
- error interceptor を chain し終わった後、`throwOnError === true` なら最終形を rethrow。`false` (default) なら `{ data: undefined, error, response }` で resolve。

**(b) RequestResult の型 (verbatim)**
```ts
export type RequestResult<TData, TError, ThrowOnError extends boolean = boolean> =
  ThrowOnError extends true
    ? Promise<{ data: TData; response: Response }>
    : Promise<
        ({ data: TData; error: undefined } | { data: undefined; error: TError })
        & { response?: Response } // network 失敗時は undefined
      >;
```

**(c) FastAPI ApiError 正規化の hook 場所**
- 推奨: **error interceptor で `ApiError` インスタンス化**。throwOnError true なら呼び元で `instanceof ApiError` で受け取れる。
- 既存 `normalizeErrorDetail()` (FastAPI HTTPException + Pydantic ValidationError 両 shape 吸収) は interceptor 内で再利用すれば差分最小。

```ts
// 想定実装
client.interceptors.error.use(async (error, response) => {
  const status = response?.status ?? 0;
  const detail = (error && typeof error === 'object' && 'detail' in error)
    ? (error as { detail: unknown }).detail
    : error;
  return new ApiError(status, normalizeErrorDetail(detail));
});

// SDK 利用側
try {
  const { data } = await getApiV1Articles({ throwOnError: true });
  return data;
} catch (e) {
  if (e instanceof ApiError) { /* ... */ }
  throw e;
}
```

**(d) 既知 issue: 型反映の不整合**
- #1565: experimental parser で `throwOnError: true` 設定時に response の data 型が `T | undefined` のまま narrow されないバグ報告あり (legacy parser では narrow されていた)。
- → 移行 PoC で実際に narrowing が効いているか **要 verify**。効かない場合は `as` キャスト or per-call `throwOnError: true` で凌ぐ。

### 不明点
- v0.97.x で #1565 が修正済か未確認 (changelog に "throwOnError option is now genuinely respected across all scenarios" の記述はあるが parser 別かは不明)。

---

## 5. custom fetch slot

### 公式 doc URL
- https://heyapi.dev/openapi-ts/clients/next-js
- ソース: `bundle/types.ts` `Config.fetch?: typeof fetch`

### 検証結果サマリ
- `Config` の `fetch?: typeof fetch` で **per-client な fetch 上書きが可能**。`globalThis.fetch` 全体差し替えは不要 (clean な per-client 注入)。
- 用途: タイムアウト + AbortSignal merge、retry、telemetry tap など。

```ts
// Vector の現行 fetcher.ts (10s timeout + signal merge) を踏襲
const customFetch: typeof fetch = async (input, init) => {
  const timeoutController = new AbortController();
  const timeoutId = setTimeout(() => timeoutController.abort(), 10_000);
  const merged = init?.signal
    ? AbortSignal.any([init.signal, timeoutController.signal])
    : timeoutController.signal;
  try {
    return await fetch(input, { ...init, signal: merged });
  } finally {
    clearTimeout(timeoutId);
  }
};

client.setConfig({ fetch: customFetch });
```

- runtimeConfigPath で `createClientConfig` 内に `fetch: customFetch` を書く形でも可。

### 不明点
- なし。

---

## 6. ValidationError schema 名の継承

### 公式 doc URL
- https://heyapi.dev/openapi-ts/plugins/typescript

### 検証結果サマリ
- `@hey-api/typescript` の生成型 3 カテゴリ: **Requests** (`AddPetData`) / **Responses** (`AddPetResponses`, `AddPetResponse`) / **Definitions** (`Pet` のような schema 由来型)。
- Definitions は schema 名を **そのまま PascalCase で export**。FastAPI が出力する `ValidationError` schema は **`ValidationError` 型としてそのまま export される**。
- ただし openapi-typescript の `components["schemas"]["ValidationError"]` のような indexed access ではなく、**named export `import type { ValidationError } from '@/types/types.gen'`** に切り替わる。
- name customization は `plugin: { name: '@hey-api/typescript', case: 'PascalCase' }` で全体規則を制御可能。
- internal alias (`_StoryOut` のような Pydantic v2 input/output 分離による suffix) は **schema 側がそう名付けていれば** そのまま反映される。Vector backend では FastAPI が `Input`/`Output` suffix を付与するケース有り (json_schema_mode_override 未指定時) なので **要事前確認**。

### 不明点
- Pydantic v2 の `-Input`/`-Output` 自動 suffix が現行 backend schema に存在するかは未確認。
- → `cd backend && uv run python -m app.main` 等で openapi.json を生成し `grep -E '(Input|Output)$' openapi.json` で suffix 有無を確認すること。

---

## 7. SDK 関数の呼び出し pattern

### 公式 doc URL
- https://heyapi.dev/openapi-ts/output/sdk
- https://heyapi.dev/openapi-ts/plugins/sdk
- ソース: `bundle/types.ts` `Options<TData>` 型

### 検証結果サマリ (実装型確認済み)

生成 SDK 関数のシグネチャ:
```ts
export const getArticle = <ThrowOnError extends boolean = false>(
  options: Options<GetArticleData, ThrowOnError>,
): RequestResult<GetArticleResponse, GetArticleError, ThrowOnError>;

// GetArticleData (TypeScript plugin が生成)
export type GetArticleData = {
  body?: never;
  path: { article_id: string };       // ← path-param は path key
  query?: { include?: 'authors' };    // ← query-string は query key
  url: '/api/v1/articles/{article_id}';
};
```

**path / query / body の渡し方** (公式 + 実装一致):
```ts
const { data } = await getArticle({
  path: { article_id: id },           // ← path-param
  query: { include: 'authors' },      // ← query
  // body: { ... }                    // ← POST 系
  next: { tags: [cacheTags.article(id)] }, // ← Next.js fetch options もここに
  throwOnError: true,
});
```

**return 型** (RequestResult):
- `throwOnError: false` (default): `{ data: T, error: undefined } | { data: undefined, error: E }` の判別 union + `response?: Response`。
- `throwOnError: true`: `{ data: T, response: Response }` のみ (data 必ず定義、ただし #1565 の narrowing バグ要確認)。

### Flat (default) vs Class instance
- デフォルトは flat function export (`getArticle`, `listArticles`, ...)。tree-shake 可能、Vector の現行 path-keyed access より import が軽い。
- `asClass: true` で class instance 化も可能 (採用しない)。

### 不明点
- query-string serializer のデフォルトは `style: 'form', explode: true` (utils.ts 確認済)。FastAPI の List query (?tags=a&tags=b) に互換。

---

## 8. cache control / Next.js options の素通し

### 公式 doc URL
- https://heyapi.dev/openapi-ts/clients/next-js (一部言及)
- ソース: `bundle/types.ts` `Config extends Omit<RequestInit, ...>`

### 検証結果サマリ
- 既に **Section 2-(d)** で確認の通り、`next: { tags, revalidate }` および `cache: 'force-cache' | 'no-store'` などは **RequestInit 由来でそのまま型に乗る**。SDK 関数呼び出しの options に乗せれば fetch まで素通し。
- 個別呼び出しごとに per-call で渡せるし、`createClientConfig` でグローバル default も設定可能。
- per-call の優先順は `..._config` → `...options` の spread なので **per-call の next/cache が default を上書き**。

```ts
// per-call (推奨パターン)
await getApiV1WatchlistMe({
  next: { tags: [cacheTags.watchlistMe], revalidate: 60 },
});

// global default (慎重に。すべての fetch に適用)
client.setConfig({
  next: { revalidate: 30 },
  cache: 'force-cache',
});
```

### 不明点
- 公式 doc に明示的な「next options pass-through」の記述は薄い。実装に基づく確認となるが、`extends Omit<RequestInit>` 構造から将来 breaking なく削除されるリスクは低い。

---

## 9. 既知の地雷

### 9.1 名前衝突
- **`@hey-api/openapi-ts`** (本パッケージ) と **`openapi-typescript` / `openapi-ts.dev` (drwpow)** は別物。前者の bin は `openapi-ts`、後者の bin は `openapi-typescript`。混乱しがち。
- `@hey-api/openapi-typescript` という名前は無い (typescript plugin は `@hey-api/typescript`)。

### 9.2 v0.x → v1.x の breaking
- 現行は **v0.97.1**、v1.0 は未到達。**0.x のうちは breaking change を覚悟**。pin (-E) 必須。
- 直近の breaking (migration guide):
  - **v0.97.0**: `runtimeConfigPath` の解像度が output 相対へ変更 / error interceptor が前段の結果を受ける chain 化 / throwOnError が「全 scenario で genuinely respected」。
  - **v0.95.0**: Node 22.13 へ bump、validator schema 構造変更。
  - **v0.54 周辺**: plugin システム導入で options 構造が完全リフレッシュ (`types.* / services.*` → `plugins: [...]`)。

### 9.3 Next.js 16 + Cache Components 環境の既知 issue
- **hey-api 側で Cache Components 関連の active issue は確認できず** (hey-api/openapi-ts repo issue 検索で 0 件)。client-next 自体が cookies()/headers() を呼ばないため、構造的に Cache Components とは衝突しない。
- 周辺: Next.js 16 #85240 ("use cache" が dynamic routes で無視) は Next.js 側の既知 bug。hey-api とは無関係。

### 9.4 throwOnError 型 narrowing (#1565)
- experimental parser で `throwOnError: true` 時に response の `data: T | undefined` が narrow されないバグ報告あり。v0.97.x で修正済かは要 verify。**移行 PoC の最初に 1 endpoint だけ生成して確認すること**。

### 9.5 SDK function の `throwOnError` 既定
- 各 SDK 関数の generic は `<ThrowOnError extends boolean = false>` (default false)。`throwOnError: true` を per-call で書くか、`createClientConfig` で `throwOnError: true` をグローバル default にする。**SDK plugin level の throwOnError 設定は型に反映されない**ことが #1565 の症状。

### 9.6 Pydantic v2 の Input/Output suffix
- FastAPI + Pydantic v2 で validation/serialization で別 schema を要する場合、自動で `-Input` / `-Output` suffix が付く。これがそのまま `types.gen.ts` の型名に反映される可能性あり。Vector の現行 schema に該当があるか **移行前に grep で確認**。

---

## Stage 2 プランへのブロッカー / 仕様変更必要点

### ブロッカーなし
全 9 項目を踏まえ、Vector frontend の Stage 2 移行は **構造的にブロッカーなし**。

### 仕様変更が必要な点

1. **error 正規化の hook 位置の移動**
   - 現状: `fetcher.ts` の `parseError()` 相当で `ApiError` を投げている (推測)。
   - 移行後: `client.interceptors.error.use(async (error, response) => new ApiError(...))` 一箇所に集約。
   - `normalizeErrorDetail()` は再利用可能。

2. **per-call の next.tags 注入方法の更新**
   - 現状: `apiFetch(path, { next: { tags: [...] } })` のような wrapping。
   - 移行後: SDK 関数の options 引数に `next: { tags: [...] }` を直接渡す。同等 API なので置換は機械的。

3. **path-keyed access (`paths["/api/v1/..."]`) → named SDK function への置換**
   - openapi-fetch の `client.GET("/api/v1/articles/{id}", { params: { path: { id }}})` 形式 → hey-api `getArticle({ path: { article_id: id }})` 形式。
   - URL を string で書く query lookup pattern が無くなり、関数名で参照する形に変わる。**全呼び出し箇所の書き換え必須**。
   - 件数調査が必要 (`grep -r 'client\.\(GET\|POST\|PUT\|PATCH\|DELETE\)' frontend/src` で実件数を把握すべき)。

4. **生成ファイル参照パスの更新**
   - 現状: `import type { paths, components } from '@/types/openapi'`。
   - 移行後: `import { getArticle, postWatchlist } from '@/types/sdk.gen'` + `import type { Article, ValidationError } from '@/types/types.gen'`。
   - `components["schemas"]["X"]` → `X` named import。**全 schema 参照箇所の書き換え必須**。

5. **`gen-types` skill の更新**
   - 現状: `openapi-typescript` で 1 ファイル生成。
   - 移行後: `openapi-ts` で 4 ファイル + 2 サブフォルダ生成。`.claude/skills/gen-types/` の手順差し替え。

6. **runtimeConfigPath ファイル新設**
   - `frontend/src/lib/api/hey-api.config.ts` 等に `createClientConfig` を export。`baseUrl`, `customFetch`, `interceptor` 登録、グローバル `throwOnError: true` 既定の設定をここに集約。

### Stage 2 PoC で必ず verify すべき項目 (実コードに触る前に)

- [ ] **#1565 の throwOnError 型 narrowing が v0.97.1 で解消されているか** (1 endpoint で `data: T` (undefined なし) になるか、`data: T | undefined` か)
- [ ] **`next: { tags }` が SDK 関数 call 経路で実際に Next.js cache に効くか** (revalidateTag で invalidation が走るか E2E で確認)
- [ ] **Pydantic Input/Output suffix の有無** (`grep -E '(Input|Output)":' backend/openapi.json` 相当)
- [ ] **既存 `cacheTags`/`ApiError`/`normalizeErrorDetail` の API contract が維持されるか** (interceptor 移植後の型合致)

---

## 参照リンク (主要)

- 公式 doc:
  - Get Started: https://heyapi.dev/openapi-ts/get-started
  - Configuration: https://heyapi.dev/openapi-ts/configuration
  - Output: https://heyapi.dev/openapi-ts/output
  - Next.js Client: https://heyapi.dev/openapi-ts/clients/next-js
  - Fetch Client: https://heyapi.dev/openapi-ts/clients/fetch
  - SDK Plugin: https://heyapi.dev/openapi-ts/plugins/sdk
  - TypeScript Plugin: https://heyapi.dev/openapi-ts/plugins/typescript
  - Migrating: https://heyapi.dev/openapi-ts/migrating

- GitHub:
  - Repo: https://github.com/hey-api/openapi-ts
  - client-next bundle ソース: `packages/openapi-ts/src/plugins/@hey-api/client-next/bundle/{client.ts,types.ts,utils.ts}`
  - Discussion #740 (throwOnError): https://github.com/orgs/hey-api/discussions/740
  - Issue #1565 (throwOnError 型 narrowing): https://github.com/hey-api/openapi-ts/issues/1565
  - Issue #1515 (Next.js client roadmap): https://github.com/hey-api/openapi-ts/issues/1515
  - Issue #914 (throw on non-success): https://github.com/hey-api/openapi-ts/issues/914
  - Issue #680 (client-fetch throw 挙動): https://github.com/hey-api/openapi-ts/issues/680
