import { describe, expect, it } from "vitest";
import type { ArticleQuery } from "@/types";
import { buildDashboardCategoryHref } from "./paper-hrefs";

describe("buildDashboardCategoryHref", () => {
  it("resets page and keeps sortOrder/perPage when changing category", () => {
    const query: ArticleQuery = {
      category: "ai",
      page: 3,
      perPage: 48,
      sortOrder: "asc",
    };

    expect(buildDashboardCategoryHref({ category: "security", query })).toBe(
      "/?category=security&sortOrder=asc&perPage=48",
    );
  });

  it("omits category and page for the all-category link", () => {
    const query: ArticleQuery = {
      category: "space",
      page: 2,
      perPage: 24,
      sortOrder: "desc",
    };

    expect(buildDashboardCategoryHref({ query })).toBe(
      "/?sortOrder=desc&perPage=24",
    );
  });

  it("returns the pathname when no preserved params remain", () => {
    expect(
      buildDashboardCategoryHref({ pathname: "/newsroom", query: {} }),
    ).toBe("/newsroom");
  });
});
