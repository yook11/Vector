import { describe, expect, it, vi } from "vitest";
import { addToWatchlistCore, removeFromWatchlistCore } from "./watchlist-cores";

describe("addToWatchlistCore", () => {
  it("calls fetcher with POST /me/watchlist and JSON body containing articleId", async () => {
    const fetcher = vi.fn().mockResolvedValue(undefined);
    await addToWatchlistCore(123, fetcher);
    expect(fetcher).toHaveBeenCalledTimes(1);
    const [path, init] = fetcher.mock.calls[0] ?? [];
    expect(path).toBe("/me/watchlist");
    expect(init).toMatchObject({ method: "POST" });
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      articleId: 123,
    });
  });

  it("uses the exact articleId (no coercion)", async () => {
    const fetcher = vi.fn().mockResolvedValue(undefined);
    await addToWatchlistCore(0, fetcher);
    const init = fetcher.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(init.body as string)).toEqual({ articleId: 0 });
  });

  it("propagates fetcher rejections", async () => {
    const error = new Error("Conflict");
    const fetcher = vi.fn().mockRejectedValue(error);
    await expect(addToWatchlistCore(1, fetcher)).rejects.toBe(error);
  });
});

describe("removeFromWatchlistCore", () => {
  it("calls fetcher with DELETE /me/watchlist/{articleId}", async () => {
    const fetcher = vi.fn().mockResolvedValue(undefined);
    await removeFromWatchlistCore(456, fetcher);
    expect(fetcher).toHaveBeenCalledWith("/me/watchlist/456", {
      method: "DELETE",
    });
  });

  it("does not include a body (DELETE)", async () => {
    const fetcher = vi.fn().mockResolvedValue(undefined);
    await removeFromWatchlistCore(1, fetcher);
    const init = fetcher.mock.calls[0]?.[1] as RequestInit;
    expect(init.body).toBeUndefined();
  });

  it("propagates fetcher rejections", async () => {
    const error = new Error("Not Found");
    const fetcher = vi.fn().mockRejectedValue(error);
    await expect(removeFromWatchlistCore(1, fetcher)).rejects.toBe(error);
  });
});
