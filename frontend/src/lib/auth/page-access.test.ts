import { describe, expect, it } from "vitest";
import { isPublicPage } from "./page-access";

describe("公開ページの明示的な境界", () => {
  it.each([
    "/",
    "/trends",
    "/briefing",
    "/news/1",
    "/news/abc",
    "/briefing/ai",
  ])("%s は公開する", (path) => {
    expect(isPublicPage(path)).toBe(true);
  });
  it.each([
    "/reports",
    "/research",
    "/watchlist",
    "/settings",
    "/admin/users/new",
    "/newsletter",
    "/news/1/edit",
    "/briefing/ai/edit",
    "/trends/private",
  ])("%s は公開しない", (path) => {
    expect(isPublicPage(path)).toBe(false);
  });
});
