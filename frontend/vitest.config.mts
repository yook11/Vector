import react from "@vitejs/plugin-react";
import tsconfigPaths from "vite-tsconfig-paths";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react(), tsconfigPaths()],
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    globals: false,
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    coverage: {
      provider: "v8",
      reporter: ["text", "json-summary", "html"],
      // 各 path には対応する *.test.{ts,tsx} が存在する必要がある。
      // ファイル移動時はこの list を更新しないと coverage 集計から silent に外れるため、
      // PR レビュー時に対応テストの存在を確認すること。
      include: [
        "src/lib/utils/sanitize-url.ts",
        "src/lib/auth/role.ts",
        "src/lib/auth/guards.ts",
        "src/lib/auth/login-redirect-url.ts",
        "src/lib/api/error.ts",
        "src/lib/api/fetcher.ts",
        "src/lib/date.ts",
        "src/lib/proxy/csp.ts",
        "src/lib/proxy/callback-url.ts",
        "src/features/news/search-params.ts",
        "src/features/sources/api/source-cores.ts",
        "src/features/sources/schemas/source.ts",
        "src/features/watchlist/api/watchlist-cores.ts",
        "src/features/auth/components/LoginForm.tsx",
        "src/features/auth/components/RegisterForm.tsx",
        "src/features/sources/components/SourceTable.tsx",
        "src/features/news/components/SearchBar.tsx",
        "src/features/watchlist/components/WatchlistButton.tsx",
      ],
      exclude: ["**/*.test.*", "**/*.d.ts", "e2e/**"],
      // Phase 3 で CI required 化に合わせて threshold を導入。
      // Phase 2 実績 (lib 98.98% / component 99.14%) から下方バッファ -10〜-19pt で
      // 過敏な fail を避けつつ regression を CI で必ず止める。
      thresholds: {
        statements: 90,
        branches: 85,
        functions: 90,
        lines: 90,
      },
    },
  },
});
