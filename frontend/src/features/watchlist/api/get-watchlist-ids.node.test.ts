import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ session: vi.fn(), list: vi.fn() }));
vi.mock("@/lib/api/hey-api-interceptors", () => ({}));
vi.mock("@/lib/auth/guards", () => ({ getCurrentSession: mocks.session }));
vi.mock("@/types/sdk.gen", () => ({ listWatchlistIds: mocks.list }));

import { getWatchlistIds } from "./get-watchlist-ids";

beforeEach(() => vi.clearAllMocks());
describe("公開ページの保存状態", () => {
  it("未ログインでは個人APIを呼ばない", async () => {
    mocks.session.mockResolvedValue(null);
    expect(await getWatchlistIds([1, 2])).toEqual(new Set());
    expect(mocks.list).not.toHaveBeenCalled();
  });
  it("記事が無ければ個人APIを呼ばない", async () => {
    mocks.session.mockResolvedValue({ user: { id: "user-1" } });
    expect(await getWatchlistIds([])).toEqual(new Set());
    expect(mocks.list).not.toHaveBeenCalled();
  });
  it("ログイン済みなら渡した記事IDで問い合わせ、ウォッチ中のIDだけ返す", async () => {
    mocks.session.mockResolvedValue({ user: { id: "user-1" } });
    mocks.list.mockResolvedValue({ data: { ids: [1, 3] } });
    expect(await getWatchlistIds([1, 2, 3])).toEqual(new Set([1, 3]));
    expect(mocks.list).toHaveBeenCalledWith(
      expect.objectContaining({ query: { articleIds: [1, 2, 3] } }),
    );
  });
});
