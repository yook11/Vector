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
      include: [
        "src/lib/utils/sanitize-url.ts",
        "src/lib/auth/role.ts",
        "src/lib/auth/guards.ts",
        "src/lib/auth/login-redirect-url.ts",
        "src/lib/api/error.ts",
        "src/lib/search-params/server.ts",
        "src/lib/date.ts",
        "src/lib/proxy/csp.ts",
        "src/lib/proxy/callback-url.ts",
        "src/features/sources/api/source-cores.ts",
        "src/features/watchlist/api/watchlist-cores.ts",
      ],
      exclude: ["**/*.test.*", "**/*.d.ts"],
    },
  },
});
