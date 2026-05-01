import react from "@vitejs/plugin-react";
import tsconfigPaths from "vite-tsconfig-paths";
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // coverage / globals は project 横断で同一なので root に置く。
    // include / environment / setup は project 単位で分岐する (ADR-005)。
    globals: false,
    coverage: {
      provider: "v8",
      reporter: ["text", "json-summary", "html"],
      // 各 path には対応する *.test.{ts,tsx} (または *.node.test.{ts,tsx})
      // が存在する必要がある。ファイル移動時はこの list を更新しないと
      // coverage 集計から silent に外れるため、PR レビュー時に対応テストの
      // 存在を確認すること。
      include: [
        "src/lib/utils/sanitize-url.ts",
        "src/lib/utils/toast-error.ts",
        "src/lib/auth/role.ts",
        "src/lib/auth/guards.ts",
        "src/lib/auth/login-redirect-url.ts",
        "src/lib/api/error.ts",
        "src/lib/api/fetcher.ts",
        "src/lib/api/typed-server-fetcher.ts",
        "src/lib/cache/tags.ts",
        "src/lib/date.ts",
        "src/lib/proxy/csp.ts",
        "src/lib/proxy/callback-url.ts",
        "src/features/news/search-params.ts",
        "src/features/sources/api/source-cores.ts",
        "src/features/sources/schemas/source.ts",
        "src/features/auth/schemas/auth.ts",
        "src/features/watchlist/api/watchlist-cores.ts",
        "src/features/digest/page-models/weekly-trends.ts",
        "src/features/auth/components/LoginForm.tsx",
        "src/features/auth/components/RegisterForm.tsx",
        "src/features/sources/components/SourceTable.tsx",
        "src/features/news/components/SearchBar.tsx",
        "src/features/watchlist/components/WatchlistButton.tsx",
      ],
      exclude: ["**/*.test.*", "**/*.d.ts", "e2e/**"],
      // PR-Z11 で test 網拡張 (fetcher timeout/AbortSignal/408 + toast-error
      // production マスク) に伴い実測が S 99.34 / B 96.21 / F 100 / L 100 に
      // 到達したので、安全マージン -3〜-4pt で tighten。これにより regression
      // 検知力を上げつつ偶発 fail を避ける。
      thresholds: {
        statements: 95,
        branches: 92,
        functions: 95,
        lines: 95,
      },
    },
    projects: [
      {
        plugins: [react(), tsconfigPaths()],
        test: {
          name: "client",
          environment: "jsdom",
          setupFiles: ["./vitest.setup.client.ts"],
          globals: false,
          // node 専用 suffix (.node.test.ts) を client から除外して二重実行を防ぐ
          include: ["src/**/*.{test,spec}.{ts,tsx}"],
          exclude: ["src/**/*.node.{test,spec}.{ts,tsx}"],
        },
      },
      {
        plugins: [tsconfigPaths()],
        test: {
          name: "rsc",
          environment: "node",
          setupFiles: ["./vitest.setup.node.ts"],
          globals: false,
          // node project は .node.test.ts suffix のみを拾う。
          // page.tsx の async 分岐 (page-model) を node 環境で直接実行するため。
          include: ["src/**/*.node.{test,spec}.{ts,tsx}"],
        },
      },
    ],
  },
});
