import { describe, expect, it } from "vitest";
import { buildLoginCallbackUrl } from "./login-redirect-url";

describe("buildLoginCallbackUrl", () => {
  describe("falsy / malformed referer", () => {
    it("returns /auth/login for null referer", () => {
      expect(buildLoginCallbackUrl(null)).toBe("/auth/login");
    });

    it("returns /auth/login for empty string", () => {
      expect(buildLoginCallbackUrl("")).toBe("/auth/login");
    });

    it("returns /auth/login for unparseable URL", () => {
      expect(buildLoginCallbackUrl("not a url")).toBe("/auth/login");
    });
  });

  describe("internal path passthrough", () => {
    it("preserves a normal page path", () => {
      expect(buildLoginCallbackUrl("https://example.com/watchlist")).toBe(
        "/auth/login?callbackUrl=%2Fwatchlist",
      );
    });

    it("preserves the root '/'", () => {
      expect(buildLoginCallbackUrl("https://example.com/")).toBe(
        "/auth/login?callbackUrl=%2F",
      );
    });

    it("preserves query string in callbackUrl", () => {
      expect(
        buildLoginCallbackUrl("https://example.com/news?category=ai&page=2"),
      ).toBe("/auth/login?callbackUrl=%2Fnews%3Fcategory%3Dai%26page%3D2");
    });
  });

  describe("open redirect prevention", () => {
    it("strips protocol-relative path that comes via URL.pathname", () => {
      // new URL("//evil.com") は base 必須で例外。referer に直接 `//evil.com`
      // は来ないが、結果として fallback に落ちることだけ保証する。
      expect(buildLoginCallbackUrl("//evil.com")).toBe("/auth/login");
    });

    it("URL parser 経由で `//` 始まりに正規化された pathname も fallback", () => {
      // new URL("https://example.com//evil").pathname === "//evil"
      // (URL spec は pathname の重複 `/` を保持する。これは login redirect の
      //  protocol-relative bypass と同等の脅威 — 構造的に弾く必要がある)
      expect(buildLoginCallbackUrl("https://example.com//evil")).toBe(
        "/auth/login",
      );
    });

    it("treats /auth/* referer as login-loop and falls back", () => {
      expect(buildLoginCallbackUrl("https://example.com/auth/login")).toBe(
        "/auth/login",
      );
      expect(buildLoginCallbackUrl("https://example.com/auth/register")).toBe(
        "/auth/login",
      );
    });

    it("origin の差異は気にしない (pathname のみ採用)", () => {
      // Server Action の referer は frontend の URL なので origin は同一前提。
      // 万一外部 origin が来ても pathname だけ使えば構造的に安全。
      expect(buildLoginCallbackUrl("https://attacker.test/dashboard")).toBe(
        "/auth/login?callbackUrl=%2Fdashboard",
      );
    });
  });

  describe("special characters in path/query", () => {
    it("encodes spaces safely (URL parser pre-normalizes to %20)", () => {
      const result = buildLoginCallbackUrl(
        "https://example.com/news?q=hello world",
      );
      expect(result.startsWith("/auth/login?callbackUrl=")).toBe(true);
      // URL parser が `?q=hello world` を `?q=hello%20world` に正規化済 →
      // それを encodeURIComponent でさらに包んでいる。decode 1 回で URL 表現に戻る。
      const decoded = decodeURIComponent(
        result.replace("/auth/login?callbackUrl=", ""),
      );
      expect(decoded).toBe("/news?q=hello%20world");
    });
  });
});
