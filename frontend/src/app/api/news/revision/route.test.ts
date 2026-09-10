import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ getRevision: vi.fn() }));

vi.mock("server-only", () => ({}));
vi.mock("@/lib/cache/article-list-revision", () => ({
  getArticleListRevision: mocks.getRevision,
}));
vi.mock("next/server", async (importOriginal) => ({
  ...(await importOriginal<typeof import("next/server")>()),
  connection: vi.fn().mockResolvedValue(undefined),
}));

import { GET } from "./route";

beforeEach(() => mocks.getRevision.mockReturnValue("current-revision"));

describe("GET /api/news/revision", () => {
  it("識別子だけをno-storeで返す", async () => {
    const response = await GET();

    expect(response.status).toBe(200);
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    expect(await response.json()).toEqual({ revision: "current-revision" });
  });
});
