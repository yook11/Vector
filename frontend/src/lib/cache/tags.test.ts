import { describe, expect, it } from "vitest";
import { briefingCategoryTag, type CacheTag, cacheTags } from "./tags";

describe("cacheTags", () => {
  it("registry literal を固定する (改名は invalidation 互換性に直結)", () => {
    // value 自体が Server Action / fetch tag 側との contract。改名は意図的に
    // この test を更新する操作にすることで、invalidation chain 全体の整合を
    // PR 単位で見える化する。
    expect(cacheTags).toEqual({
      watchlistMe: "watchlist:me",
      sources: "sources",
      briefingList: "briefing:list",
      trends: "trends",
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

describe("briefingCategoryTag", () => {
  it("backend notifier と同じ命名規約 (briefing:<slug>) を組み立てる", () => {
    // backend `FrontendRevalidateNotifier` が打つ tag と完全一致しないと
    // on-demand revalidate 経路が silent に死ぬ。命名規約を test で固定する。
    expect(briefingCategoryTag("ai")).toBe("briefing:ai");
    expect(briefingCategoryTag("robotics")).toBe("briefing:robotics");
  });
});
