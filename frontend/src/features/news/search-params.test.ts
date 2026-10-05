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

  describe("unknown params", () => {
    it("ignores retired q search param", () => {
      expect(parseArticleQuery({ q: "openai" })).not.toHaveProperty("q");
      expect(parseArticleQuery({ q: "openai" }).query).toEqual({});
    });
  });

  it("ignores retired page / perPage / sortOrder params", () => {
    const { query } = parseArticleQuery({
      category: "ai",
      sortOrder: "asc",
      page: "2",
      perPage: "48",
    });
    expect(query).toEqual({ category: "ai" });
  });
});
