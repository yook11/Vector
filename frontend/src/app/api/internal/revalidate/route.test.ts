import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const SECRET = "test-secret-32characters-long-xxxx";

const mocks = vi.hoisted(() => ({
  updateTag: vi.fn(),
}));

vi.mock("server-only", () => ({}));
vi.mock("next/cache", () => ({
  updateTag: mocks.updateTag,
}));
vi.mock("@/lib/env", () => ({
  requireEnv: () => "test-secret-32characters-long-xxxx",
}));

import { POST } from "./route";

function buildRequest(opts: {
  authorization?: string;
  body?: unknown;
}): NextRequest {
  const headers = new Headers({ "Content-Type": "application/json" });
  if (opts.authorization !== undefined) {
    headers.set("Authorization", opts.authorization);
  }
  return opts.body !== undefined
    ? new NextRequest("http://test.local/api/internal/revalidate", {
        method: "POST",
        headers,
        body: JSON.stringify(opts.body),
      })
    : new NextRequest("http://test.local/api/internal/revalidate", {
        method: "POST",
        headers,
      });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("POST /api/internal/revalidate", () => {
  it("returns 401 when Authorization header is absent", async () => {
    const res = await POST(buildRequest({ body: { tags: ["briefing:list"] } }));
    expect(res.status).toBe(401);
    expect(mocks.updateTag).not.toHaveBeenCalled();
  });

  it("returns 403 when Bearer token does not match secret", async () => {
    const res = await POST(
      buildRequest({
        authorization: "Bearer wrong-secret",
        body: { tags: ["briefing:list"] },
      }),
    );
    expect(res.status).toBe(403);
    expect(mocks.updateTag).not.toHaveBeenCalled();
  });

  it("returns 403 when Authorization is not a Bearer scheme", async () => {
    const res = await POST(
      buildRequest({
        authorization: `Basic ${SECRET}`,
        body: { tags: ["briefing:list"] },
      }),
    );
    expect(res.status).toBe(403);
    expect(mocks.updateTag).not.toHaveBeenCalled();
  });

  it("returns 400 when body is missing tags", async () => {
    const res = await POST(
      buildRequest({
        authorization: `Bearer ${SECRET}`,
        body: { other: "field" },
      }),
    );
    expect(res.status).toBe(400);
    expect(mocks.updateTag).not.toHaveBeenCalled();
  });

  it("returns 400 when body is not valid JSON", async () => {
    const headers = new Headers({
      "Content-Type": "application/json",
      Authorization: `Bearer ${SECRET}`,
    });
    const req = new NextRequest("http://test.local/api/internal/revalidate", {
      method: "POST",
      headers,
      body: "not-json{",
    });
    const res = await POST(req);
    expect(res.status).toBe(400);
    expect(mocks.updateTag).not.toHaveBeenCalled();
  });

  it("returns 400 when tags array is empty", async () => {
    const res = await POST(
      buildRequest({
        authorization: `Bearer ${SECRET}`,
        body: { tags: [] },
      }),
    );
    expect(res.status).toBe(400);
    expect(mocks.updateTag).not.toHaveBeenCalled();
  });

  it("revalidates each tag and returns 200 on success", async () => {
    const res = await POST(
      buildRequest({
        authorization: `Bearer ${SECRET}`,
        body: { tags: ["briefing:list", "briefing:ai"] },
      }),
    );
    expect(res.status).toBe(200);
    const json = (await res.json()) as { ok: boolean; count: number };
    expect(json.ok).toBe(true);
    expect(json.count).toBe(2);
    expect(mocks.updateTag).toHaveBeenCalledTimes(2);
    expect(mocks.updateTag).toHaveBeenCalledWith("briefing:list");
    expect(mocks.updateTag).toHaveBeenCalledWith("briefing:ai");
  });
});
