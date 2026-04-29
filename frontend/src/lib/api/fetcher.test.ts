import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";
import { server } from "@/test/msw/server";
import { ApiError } from "./error";
import { requestEmpty, requestJson } from "./fetcher";

// msw の handler は test 内で `server.use(...)` で都度定義する。
// グローバル handler を持たないことで features 横断 mock 禁止と整合させる。

const TEST_URL = "http://test.local/items";

describe("requestJson — msw 経由の HTTP 経路", () => {
  it("200 + JSON body を T としてパースして返す", async () => {
    server.use(
      http.get(TEST_URL, () => HttpResponse.json({ id: 1, name: "alpha" })),
    );
    const result = await requestJson<{ id: number; name: string }>(TEST_URL);
    expect(result).toEqual({ id: 1, name: "alpha" });
  });

  it("FastAPI HTTPException (string detail) を ApiError に整形して throw", async () => {
    server.use(
      http.get(TEST_URL, () =>
        HttpResponse.json({ detail: "Resource not found" }, { status: 404 }),
      ),
    );
    await expect(requestJson(TEST_URL)).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      detail: "Resource not found",
    });
  });

  it("Pydantic validation array detail を 'field: msg' に正規化して throw", async () => {
    server.use(
      http.post(TEST_URL, () =>
        HttpResponse.json(
          {
            detail: [
              {
                loc: ["body", "email"],
                msg: "invalid format",
                type: "value_error",
              },
            ],
          },
          { status: 422 },
        ),
      ),
    );
    await expect(
      requestJson(TEST_URL, { method: "POST" }),
    ).rejects.toMatchObject({
      name: "ApiError",
      status: 422,
      detail: "email: invalid format",
    });
  });

  it("解析不能 body は statusText フォールバックで ApiError(status, statusText)", async () => {
    server.use(
      http.get(
        TEST_URL,
        () =>
          new HttpResponse("oops", {
            status: 500,
            statusText: "Internal Server Error",
          }),
      ),
    );
    const error = await requestJson(TEST_URL).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(500);
    // body は plain text で normalizeErrorDetail() が "" を返すので statusText に fallback
    expect((error as ApiError).detail).toBe("Internal Server Error");
  });
});

describe("requestEmpty — 204 No Content", () => {
  it("204 で void resolve (body を読まない)", async () => {
    server.use(
      http.delete(TEST_URL, () => new HttpResponse(null, { status: 204 })),
    );
    const result = await requestEmpty(TEST_URL, { method: "DELETE" });
    expect(result).toBeUndefined();
  });
});
