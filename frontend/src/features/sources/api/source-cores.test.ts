import { describe, expect, it, vi } from "vitest";
import type { NewsSourceCreate, NewsSourceDetail } from "@/types";
import {
  activateSourceCore,
  createSourceCore,
  deactivateSourceCore,
  deleteSourceCore,
} from "./source-cores";

const sampleDetail: NewsSourceDetail = {
  id: 1,
  name: "Example",
  sourceType: "rss",
  siteUrl: "https://example.com",
  endpointUrl: "https://example.com/feed",
  isActive: true,
  createdAt: "2026-01-01T00:00:00Z",
  updatedAt: "2026-01-01T00:00:00Z",
};

describe("activateSourceCore", () => {
  it("calls fetcher with PATCH /admin/sources/{id}/activate", async () => {
    const fetcher = vi.fn().mockResolvedValue(sampleDetail);
    const result = await activateSourceCore(42, fetcher);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledWith("/admin/sources/42/activate", {
      method: "PATCH",
    });
    expect(result).toBe(sampleDetail);
  });

  it("propagates fetcher rejections", async () => {
    const error = new Error("Forbidden");
    const fetcher = vi.fn().mockRejectedValue(error);
    await expect(activateSourceCore(1, fetcher)).rejects.toBe(error);
  });
});

describe("deactivateSourceCore", () => {
  it("calls fetcher with PATCH /admin/sources/{id}/deactivate", async () => {
    const fetcher = vi.fn().mockResolvedValue(sampleDetail);
    const result = await deactivateSourceCore(7, fetcher);
    expect(fetcher).toHaveBeenCalledWith("/admin/sources/7/deactivate", {
      method: "PATCH",
    });
    expect(result).toBe(sampleDetail);
  });

  it("uses the exact id without coercion", async () => {
    const fetcher = vi.fn().mockResolvedValue(sampleDetail);
    await deactivateSourceCore(0, fetcher);
    expect(fetcher).toHaveBeenCalledWith("/admin/sources/0/deactivate", {
      method: "PATCH",
    });
  });
});

describe("createSourceCore", () => {
  const body: NewsSourceCreate = {
    name: "New Source",
    sourceType: "rss",
    siteUrl: "https://new.example.com",
    endpointUrl: "https://new.example.com/feed",
  };

  it("calls fetcher with POST /admin/sources and JSON-stringified body", async () => {
    const fetcher = vi.fn().mockResolvedValue(sampleDetail);
    const result = await createSourceCore(body, fetcher);
    expect(fetcher).toHaveBeenCalledTimes(1);
    const [path, init] = fetcher.mock.calls[0] ?? [];
    expect(path).toBe("/admin/sources");
    expect(init).toMatchObject({ method: "POST" });
    // body は JSON 文字列 — 元 object と等価であることを構造的に検証
    expect(JSON.parse((init as RequestInit).body as string)).toEqual(body);
    expect(result).toBe(sampleDetail);
  });

  it("propagates fetcher errors", async () => {
    const error = new Error("Bad Request");
    const fetcher = vi.fn().mockRejectedValue(error);
    await expect(createSourceCore(body, fetcher)).rejects.toBe(error);
  });
});

describe("deleteSourceCore", () => {
  it("calls fetcher with DELETE /admin/sources/{id} and resolves to undefined", async () => {
    const fetcher = vi.fn().mockResolvedValue(undefined);
    const result = await deleteSourceCore(99, fetcher);
    expect(fetcher).toHaveBeenCalledWith("/admin/sources/99", {
      method: "DELETE",
    });
    expect(result).toBeUndefined();
  });

  it("propagates fetcher errors", async () => {
    const error = new Error("Not Found");
    const fetcher = vi.fn().mockRejectedValue(error);
    await expect(deleteSourceCore(99, fetcher)).rejects.toBe(error);
  });
});
