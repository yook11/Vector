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

  describe("page (numeric range)", () => {
    it("parses numeric page", () => {
      const { query } = parseArticleQuery({ page: "3" });
      expect(query.page).toBe(3);
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

    it("rejects page values outside configured bounds", () => {
      expect(parseArticleQuery({ page: "0" }).query).not.toHaveProperty("page");
      expect(parseArticleQuery({ page: "10001" }).query).not.toHaveProperty(
        "page",
      );
    });
  });

  describe("perPage (allowlist)", () => {
    it.each([
      "12",
      "24",
      "48",
      "100",
    ])("accepts allowlist value %s as number", (raw) => {
      const { query } = parseArticleQuery({ perPage: raw });
      expect(query.perPage).toBe(Number(raw));
    });

    it("rejects retired values (legacy 20 / 50 fall back to default)", () => {
      expect(parseArticleQuery({ perPage: "20" }).query).not.toHaveProperty(
        "perPage",
      );
      expect(parseArticleQuery({ perPage: "50" }).query).not.toHaveProperty(
        "perPage",
      );
    });

    it("rejects arbitrary numbers outside the allowlist", () => {
      expect(parseArticleQuery({ perPage: "30" }).query).not.toHaveProperty(
        "perPage",
      );
      expect(parseArticleQuery({ perPage: "101" }).query).not.toHaveProperty(
        "perPage",
      );
    });

    it("rejects non-numeric / array perPage", () => {
      expect(parseArticleQuery({ perPage: "abc" }).query).not.toHaveProperty(
        "perPage",
      );
      expect(
        parseArticleQuery({ perPage: ["24", "48"] }).query,
      ).not.toHaveProperty("perPage");
    });
  });

  describe("unknown params", () => {
    it("ignores retired q search param", () => {
      expect(parseArticleQuery({ q: "openai" })).not.toHaveProperty("q");
      expect(parseArticleQuery({ q: "openai" }).query).toEqual({});
    });
  });

  it("combines all params into a single query object", () => {
    const { query } = parseArticleQuery({
      category: "ai",
      sortOrder: "desc",
      page: "2",
      perPage: "48",
    });
    expect(query).toEqual({
      category: "ai",
      sortOrder: "desc",
      page: 2,
      perPage: 48,
    });
  });
});
