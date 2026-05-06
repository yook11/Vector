import { describe, expect, it } from "vitest";
import { parseArticleQuery } from "./search-params";

describe("parseArticleQuery", () => {
  describe("category", () => {
    it("includes string category", () => {
      const { query } = parseArticleQuery({ category: "ai" });
      expect(query.category).toBe("ai");
    });

    it("trims valid category slugs", () => {
      const { query } = parseArticleQuery({ category: " ai_ml " });
      expect(query.category).toBe("ai_ml");
    });

    it("omits category when not provided", () => {
      const { query } = parseArticleQuery({});
      expect(query).not.toHaveProperty("category");
    });

    it("ignores array values (Next.js delivers repeated params as arrays)", () => {
      const { query } = parseArticleQuery({ category: ["ai", "web"] });
      expect(query).not.toHaveProperty("category");
    });

    it("omits empty string category (treated as not provided)", () => {
      const { query } = parseArticleQuery({ category: "" });
      expect(query).not.toHaveProperty("category");
    });

    it("rejects category values outside the backend slug pattern", () => {
      const { query } = parseArticleQuery({ category: "../admin" });
      expect(query).not.toHaveProperty("category");
    });
  });

  describe("sortOrder", () => {
    it("accepts 'asc'", () => {
      const { query } = parseArticleQuery({ sortOrder: "asc" });
      expect(query.sortOrder).toBe("asc");
    });

    it("accepts 'desc'", () => {
      const { query } = parseArticleQuery({ sortOrder: "desc" });
      expect(query.sortOrder).toBe("desc");
    });

    it("rejects values outside the allowlist", () => {
      const { query } = parseArticleQuery({ sortOrder: "ascending" });
      expect(query).not.toHaveProperty("sortOrder");
    });

    it("rejects array sortOrder", () => {
      const { query } = parseArticleQuery({ sortOrder: ["asc"] });
      expect(query).not.toHaveProperty("sortOrder");
    });
  });

  describe("page / perPage (numeric)", () => {
    it("parses numeric page", () => {
      const { query } = parseArticleQuery({ page: "3" });
      expect(query.page).toBe(3);
    });

    it("parses numeric perPage", () => {
      const { query } = parseArticleQuery({ perPage: "50" });
      expect(query.perPage).toBe(50);
    });

    it("rejects non-numeric page (NaN)", () => {
      const { query } = parseArticleQuery({ page: "abc" });
      expect(query).not.toHaveProperty("page");
    });

    it("rejects empty string page", () => {
      const { query } = parseArticleQuery({ page: "" });
      expect(query).not.toHaveProperty("page");
    });

    it("ignores array page values", () => {
      const { query } = parseArticleQuery({ page: ["1", "2"] });
      expect(query).not.toHaveProperty("page");
    });

    it("rejects decimal and exponent notation", () => {
      expect(parseArticleQuery({ page: "1.5" }).query).not.toHaveProperty(
        "page",
      );
      expect(parseArticleQuery({ page: "1e2" }).query).not.toHaveProperty(
        "page",
      );
    });

    it("rejects values outside configured bounds", () => {
      expect(parseArticleQuery({ page: "0" }).query).not.toHaveProperty("page");
      expect(parseArticleQuery({ page: "10001" }).query).not.toHaveProperty(
        "page",
      );
      expect(parseArticleQuery({ perPage: "101" }).query).not.toHaveProperty(
        "perPage",
      );
    });
  });

  describe("q (search term)", () => {
    it("returns string q passthrough", () => {
      const { q } = parseArticleQuery({ q: "openai" });
      expect(q).toBe("openai");
    });

    it("trims q and rejects blank values", () => {
      expect(parseArticleQuery({ q: "  openai  " }).q).toBe("openai");
      expect(parseArticleQuery({ q: "   " }).q).toBeUndefined();
    });

    it("rejects oversized q values", () => {
      const { q } = parseArticleQuery({ q: "a".repeat(201) });
      expect(q).toBeUndefined();
    });

    it("returns undefined when q is array", () => {
      const { q } = parseArticleQuery({ q: ["a", "b"] });
      expect(q).toBeUndefined();
    });

    it("returns undefined when q is missing", () => {
      const { q } = parseArticleQuery({});
      expect(q).toBeUndefined();
    });
  });

  it("combines all params into a single query object", () => {
    const { query, q } = parseArticleQuery({
      category: "ai",
      sortOrder: "desc",
      page: "2",
      perPage: "20",
      q: "claude",
    });
    expect(query).toEqual({
      category: "ai",
      sortOrder: "desc",
      page: 2,
      perPage: 20,
    });
    expect(q).toBe("claude");
  });
});
