import { defineConfig } from "@hey-api/openapi-ts";

/**
 * @hey-api/openapi-ts 設定。`npm run generate-types` で実行される。
 *
 * input: backend が起動している前提で `/openapi.json` を直接取りに行く。
 *   オフライン or backend port 未公開時は `/tmp/vector-openapi.json` 等の
 *   ファイルパスに切り替えて使う (gen-types skill で手順を案内)。
 *
 * output:
 *   - clean: false — `src/types/index.ts` (手書き narrowing 集約) と旧
 *     `generated.ts` を削除しないため必須。default の clean: true は dir
 *     全削除する破壊的挙動。
 *   - entryFile: false — hey-api 自動生成 `index.ts` (全 export を再 export)
 *     を抑止し、手書き `index.ts` を保護する。
 *
 * plugins:
 *   - @hey-api/typescript: schema 型 (types.gen.ts)
 *   - @hey-api/sdk: 関数群 (sdk.gen.ts)
 *   - @hey-api/client-next: Next.js 用 fetch wrapper (client.gen.ts + core/)。
 *     runtimeConfigPath で baseUrl + customFetch + interceptor 登録を
 *     `src/lib/api/hey-api.config.ts` に集約する。**.ts 拡張子は付けない** —
 *     付けると生成 import が `from '../lib/api/hey-api.config.ts'` になり、
 *     TS5097 (allowImportingTsExtensions) で `tsc --noEmit` が落ちる。
 *     `moduleResolution: "bundler"` 配下なら拡張子なしで `.ts` に解決される。
 */
export default defineConfig({
  input: process.env.OPENAPI_INPUT ?? "http://localhost:8000/openapi.json",
  output: {
    path: "src/types",
    clean: false,
    entryFile: false,
  },
  plugins: [
    "@hey-api/typescript",
    "@hey-api/sdk",
    {
      name: "@hey-api/client-next",
      runtimeConfigPath: "./src/lib/api/hey-api.config",
    },
  ],
});
