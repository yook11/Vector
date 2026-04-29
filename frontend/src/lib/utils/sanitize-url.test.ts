import { describe, expect, it } from "vitest";
import { sanitizeUrl } from "./sanitize-url";

describe("sanitizeUrl", () => {
  describe("safe protocols", () => {
    it("returns href for http URLs", () => {
      expect(sanitizeUrl("http://example.com/path")).toBe(
        "http://example.com/path",
      );
    });

    it("returns href for https URLs", () => {
      expect(sanitizeUrl("https://example.com/path?q=1")).toBe(
        "https://example.com/path?q=1",
      );
    });

    it("normalizes URL via URL parser (trailing slash on origin only)", () => {
      // new URL("https://example.com").href adds the canonical trailing slash
      expect(sanitizeUrl("https://example.com")).toBe("https://example.com/");
    });
  });

  describe("unsafe protocols", () => {
    it.each([
      ["javascript:", "javascript:alert(1)"],
      ["data:", "data:text/html,<script>alert(1)</script>"],
      ["vbscript:", "vbscript:msgbox(1)"],
      ["file:", "file:///etc/passwd"],
    ])("rejects %s scheme", (_, url) => {
      expect(sanitizeUrl(url)).toBeNull();
    });

    it("rejects javascript: with http suffix trick", () => {
      // Substring check (`startsWith("http")`) would miss this; URL parser catches it.
      expect(sanitizeUrl("javascript:alert(1)//http://example.com")).toBeNull();
    });
  });

  describe("invalid input", () => {
    it("returns null for empty string", () => {
      expect(sanitizeUrl("")).toBeNull();
    });

    it("returns null for malformed URL without protocol", () => {
      expect(sanitizeUrl("not a url")).toBeNull();
    });

    it("returns null for protocol-relative URL (no base)", () => {
      expect(sanitizeUrl("//evil.com/path")).toBeNull();
    });
  });
});
