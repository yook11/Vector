import { describe, expect, it } from "vitest";
import { type CacheTag, cacheTags } from "./tags";

describe("cacheTags", () => {
  it("registry literal を固定する (改名は invalidation 互換性に直結)", () => {
    // value 自体が Server Action / fetch tag 側との contract。改名は意図的に
    // この test を更新する操作にすることで、invalidation chain 全体の整合を
    // PR 単位で見える化する。
    expect(cacheTags).toEqual({
      watchlistMe: "watchlist:me",
      sources: "sources",
    });
  });

  it("型は literal union として narrow される", () => {
    const t: CacheTag = cacheTags.sources;
    // @ts-expect-error 任意 string は CacheTag に代入できない (typo 防止)
    const bad: CacheTag = "typo";
    expect(t).toBe("sources");
    void bad;
  });
});
