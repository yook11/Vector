import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

vi.mock("@/lib/api/internal-config", () => ({
  INTERNAL_API_URL: "http://test.local/api/v1",
}));

import { InternalFetchError } from "@/lib/api/error";
import { createClientConfig } from "./hey-api.config";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function configuredFetch(): typeof fetch {
  const fetchImpl = createClientConfig({}).fetch;
  if (!fetchImpl) {
    throw new Error("custom fetch not configured");
  }
  return fetchImpl;
}

describe("createClientConfig customFetch", () => {
  it("wraps network errors as InternalFetchError", async () => {
    const fetchMock: typeof fetch = async () => {
      throw new TypeError("fetch failed");
    };
    vi.stubGlobal("fetch", fetchMock);

    const err = await configuredFetch()(
      "http://test.local/api/v1/articles",
    ).catch((caught: unknown) => caught);

    expect(err).toBeInstanceOf(InternalFetchError);
    expect((err as InternalFetchError).kind).toBe("network");
    expect((err as InternalFetchError).message).toBe("fetch failed");
  });

  it("wraps timeout aborts as InternalFetchError", async () => {
    vi.useFakeTimers();
    const fetchMock: typeof fetch = (_input, init) =>
      new Promise<Response>((_resolve, reject) => {
        const signal = init?.signal;
        if (!signal) {
          reject(new Error("missing signal"));
          return;
        }
        signal.addEventListener("abort", () => {
          const err = new Error("aborted");
          err.name = "AbortError";
          reject(err);
        });
      });
    vi.stubGlobal("fetch", fetchMock);

    const promise = configuredFetch()(
      "http://test.local/api/v1/articles",
    ).catch((caught: unknown) => caught);
    await vi.advanceTimersByTimeAsync(10_000);
    const err = await promise;

    expect(err).toBeInstanceOf(InternalFetchError);
    expect((err as InternalFetchError).kind).toBe("timeout");
    expect((err as InternalFetchError).message).toBe(
      "Request timeout after 10000ms",
    );
  });
});
