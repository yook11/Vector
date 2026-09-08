import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const SECRET = "test-secret-32characters-long-xxxx";

const mocks = vi.hoisted(() => ({
  revalidateTag: vi.fn(),
}));

vi.mock("server-only", () => ({}));
vi.mock("next/cache", () => ({
  revalidateTag: mocks.revalidateTag,
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
    expect(mocks.revalidateTag).not.toHaveBeenCalled();
  });

  it("returns 403 when Bearer token does not match secret", async () => {
    const res = await POST(
      buildRequest({
        authorization: "Bearer wrong-secret",
        body: { tags: ["briefing:list"] },
      }),
    );
    expect(res.status).toBe(403);
    expect(mocks.revalidateTag).not.toHaveBeenCalled();
  });

  it("returns 403 when Authorization is not a Bearer scheme", async () => {
    const res = await POST(
      buildRequest({
        authorization: `Basic ${SECRET}`,
        body: { tags: ["briefing:list"] },
      }),
    );
    expect(res.status).toBe(403);
    expect(mocks.revalidateTag).not.toHaveBeenCalled();
  });

  it("returns 400 when body is missing tags", async () => {
    const res = await POST(
      buildRequest({
        authorization: `Bearer ${SECRET}`,
        body: { other: "field" },
      }),
    );
    expect(res.status).toBe(400);
    expect(mocks.revalidateTag).not.toHaveBeenCalled();
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
    expect(mocks.revalidateTag).not.toHaveBeenCalled();
  });

  it("returns 400 when tags array is empty", async () => {
    const res = await POST(
      buildRequest({
        authorization: `Bearer ${SECRET}`,
        body: { tags: [] },
      }),
    );
    expect(res.status).toBe(400);
    expect(mocks.revalidateTag).not.toHaveBeenCalled();
  });

  it.each([
    ["briefing:list", "briefing:ai"],
    ["articles:list", "articles:categories"],
    ["trends", "briefing:list"],
  ])("expires %s and %s immediately", async (first, second) => {
    const res = await POST(
      buildRequest({
        authorization: `Bearer ${SECRET}`,
        body: { tags: [first, second] },
      }),
    );
    expect(res.status).toBe(200);
    const json = (await res.json()) as { ok: boolean; count: number };
    expect(json.ok).toBe(true);
    expect(json.count).toBe(2);
    expect(mocks.revalidateTag).toHaveBeenCalledTimes(2);
    expect(mocks.revalidateTag).toHaveBeenCalledWith(first, { expire: 0 });
    expect(mocks.revalidateTag).toHaveBeenCalledWith(second, { expire: 0 });
  });
});
