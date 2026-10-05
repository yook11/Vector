import { describe, expect, it } from "vitest";
import { buildDashboardCategoryHref } from "./dashboard-hrefs";

describe("buildDashboardCategoryHref", () => {
  it("links to the selected category", () => {
    expect(buildDashboardCategoryHref({ category: "security" })).toBe(
      "/?category=security",
    );
  });

  it("links to the pathname for the all-category link", () => {
    expect(buildDashboardCategoryHref({})).toBe("/");
  });

  it("keeps a custom pathname", () => {
    expect(
      buildDashboardCategoryHref({ category: "ai", pathname: "/newsroom" }),
    ).toBe("/newsroom?category=ai");
  });
});
