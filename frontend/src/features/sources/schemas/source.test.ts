import { describe, expect, it } from "vitest";
import { NewSourceSchema, SourceTypeSchema } from "./source";

const VALID_INPUT = {
  name: "TechCrunch",
  sourceType: "rss",
  siteUrl: "https://techcrunch.com",
  endpointUrl: "https://techcrunch.com/feed/",
} as const;

describe("SourceTypeSchema", () => {
  it("accepts 'rss' and 'api'", () => {
    expect(SourceTypeSchema.parse("rss")).toBe("rss");
    expect(SourceTypeSchema.parse("api")).toBe("api");
  });

  it("rejects values outside the SourceType enum", () => {
    expect(SourceTypeSchema.safeParse("html").success).toBe(false);
    expect(SourceTypeSchema.safeParse("").success).toBe(false);
    expect(SourceTypeSchema.safeParse(null).success).toBe(false);
  });
});

describe("NewSourceSchema", () => {
  it("accepts a fully valid payload", () => {
    const result = NewSourceSchema.safeParse(VALID_INPUT);
    expect(result.success).toBe(true);
  });

  describe("name", () => {
    it("trims surrounding whitespace before length check", () => {
      const result = NewSourceSchema.safeParse({
        ...VALID_INPUT,
        name: "  TechCrunch  ",
      });
      expect(result.success).toBe(true);
      if (result.success) expect(result.data.name).toBe("TechCrunch");
    });

    it("rejects empty / whitespace-only name", () => {
      expect(
        NewSourceSchema.safeParse({ ...VALID_INPUT, name: "" }).success,
      ).toBe(false);
      expect(
        NewSourceSchema.safeParse({ ...VALID_INPUT, name: "   " }).success,
      ).toBe(false);
    });

    it("rejects names exceeding 50 characters", () => {
      const result = NewSourceSchema.safeParse({
        ...VALID_INPUT,
        name: "a".repeat(51),
      });
      expect(result.success).toBe(false);
    });

    it("accepts unicode word characters (e.g. Japanese)", () => {
      const result = NewSourceSchema.safeParse({
        ...VALID_INPUT,
        name: "テックニュース",
      });
      expect(result.success).toBe(true);
    });

    it("rejects characters outside the allowed set", () => {
      // backend `SourceName` pattern も同等に拒否する: `!`, `?`, `<`, `>` 等
      expect(
        NewSourceSchema.safeParse({ ...VALID_INPUT, name: "Tech!Crunch" })
          .success,
      ).toBe(false);
      expect(
        NewSourceSchema.safeParse({ ...VALID_INPUT, name: "<script>" }).success,
      ).toBe(false);
    });

    it("requires at least one word character (symbols-only is rejected)", () => {
      const result = NewSourceSchema.safeParse({ ...VALID_INPUT, name: "---" });
      expect(result.success).toBe(false);
    });
  });

  describe("sourceType", () => {
    it("rejects an invalid sourceType", () => {
      const result = NewSourceSchema.safeParse({
        ...VALID_INPUT,
        sourceType: "html",
      });
      expect(result.success).toBe(false);
    });
  });

  describe("siteUrl / endpointUrl", () => {
    it("rejects non-http(s) schemes (defense-in-depth)", () => {
      // backend SafeUrl も AnyHttpUrl で同等の拒否を行う
      for (const bad of [
        "javascript:alert(1)",
        "data:text/html,<script>",
        "ftp://example.com",
        "file:///etc/passwd",
      ]) {
        expect(
          NewSourceSchema.safeParse({ ...VALID_INPUT, siteUrl: bad }).success,
        ).toBe(false);
      }
    });

    it("accepts http and https URLs", () => {
      expect(
        NewSourceSchema.safeParse({
          ...VALID_INPUT,
          siteUrl: "http://example.com",
          endpointUrl: "https://example.com/feed",
        }).success,
      ).toBe(true);
    });

    it("rejects URLs longer than 2048 characters", () => {
      const longUrl = `https://example.com/${"a".repeat(2050)}`;
      const result = NewSourceSchema.safeParse({
        ...VALID_INPUT,
        siteUrl: longUrl,
      });
      expect(result.success).toBe(false);
    });

    it("rejects malformed URL strings", () => {
      expect(
        NewSourceSchema.safeParse({ ...VALID_INPUT, siteUrl: "not a url" })
          .success,
      ).toBe(false);
      expect(
        NewSourceSchema.safeParse({ ...VALID_INPUT, siteUrl: "" }).success,
      ).toBe(false);
    });
  });
});
