import { describe, expect, it } from "vitest";
import { ApiError, InternalFetchError, normalizeErrorDetail } from "./error";

describe("ApiError", () => {
  it("exposes status and detail, sets name to 'ApiError'", () => {
    const err = new ApiError(404, "Not found");
    expect(err.status).toBe(404);
    expect(err.detail).toBe("Not found");
    expect(err.message).toBe("Not found");
    expect(err.name).toBe("ApiError");
    expect(err).toBeInstanceOf(Error);
  });

  it("keeps optional diagnostic meta without breaking two-arg calls", () => {
    const err = new ApiError(0, "fetch failed", {
      kind: "network",
      method: "GET",
      path: "/api/v1/articles",
    });
    expect(err.meta).toEqual({
      kind: "network",
      method: "GET",
      path: "/api/v1/articles",
    });
  });
});

describe("InternalFetchError", () => {
  it("exposes kind, message, and Error name", () => {
    const err = new InternalFetchError("timeout", "Request timeout");
    expect(err.kind).toBe("timeout");
    expect(err.message).toBe("Request timeout");
    expect(err.name).toBe("InternalFetchError");
    expect(err).toBeInstanceOf(Error);
  });
});

describe("normalizeErrorDetail", () => {
  describe("string detail (HTTPException)", () => {
    it("returns the string as-is", () => {
      expect(normalizeErrorDetail({ detail: "Resource not found" })).toBe(
        "Resource not found",
      );
    });
  });

  describe("array detail (Pydantic validation)", () => {
    it("formats single error as 'field: msg' with source prefix stripped", () => {
      expect(
        normalizeErrorDetail({
          detail: [{ loc: ["query", "page"], msg: "must be positive" }],
        }),
      ).toBe("page: must be positive");
    });

    it("joins multiple errors with '; '", () => {
      expect(
        normalizeErrorDetail({
          detail: [
            { loc: ["query", "page"], msg: "must be positive" },
            { loc: ["query", "perPage"], msg: "must be <= 100" },
          ],
        }),
      ).toBe("page: must be positive; perPage: must be <= 100");
    });

    it("joins nested loc parts with '.'", () => {
      expect(
        normalizeErrorDetail({
          detail: [{ loc: ["body", "user", "email"], msg: "invalid format" }],
        }),
      ).toBe("user.email: invalid format");
    });

    it("falls back to msg only when loc has no field part", () => {
      expect(
        normalizeErrorDetail({
          detail: [{ loc: ["body"], msg: "missing" }],
        }),
      ).toBe("missing");
    });

    it("skips entries with neither field nor msg", () => {
      expect(
        normalizeErrorDetail({
          detail: [{ loc: ["query", "ok"], msg: "bad" }, null, { foo: "bar" }],
        }),
      ).toBe("ok: bad");
    });
  });

  describe("malformed body", () => {
    it("returns '' for null body", () => {
      expect(normalizeErrorDetail(null)).toBe("");
    });

    it("returns '' for non-object body (string)", () => {
      expect(normalizeErrorDetail("oops")).toBe("");
    });

    it("returns '' for non-object body (number)", () => {
      expect(normalizeErrorDetail(500)).toBe("");
    });

    it("returns '' for object without 'detail' key", () => {
      expect(normalizeErrorDetail({ message: "no detail key" })).toBe("");
    });

    it("returns '' for detail of unsupported type (number)", () => {
      expect(normalizeErrorDetail({ detail: 42 })).toBe("");
    });
  });
});
