import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type {
  BriefingListViewModel,
  PendingCategory,
  ReadyBriefingCard,
} from "../page-models/briefing-list";
import { BriefingIndexView } from "./BriefingIndexView";

// ---------------------------------------------------------------------------
// Fixture helpers
// ---------------------------------------------------------------------------

function makeReadyCard(
  overrides: Partial<ReadyBriefingCard> = {},
): ReadyBriefingCard {
  return {
    category: { slug: "ai", name: "AI" },
    weekStart: "2026-06-02",
    headline: "AI の最前線",
    summary: "今週のAI動向",
    inputArticleCount: 10,
    ...overrides,
  };
}

function makeViewModel(
  overrides: Partial<BriefingListViewModel> = {},
): BriefingListViewModel {
  return {
    weekStart: "2026-06-02",
    weekEnd: "2026-06-08",
    totalArticles: 0,
    ready: [],
    pending: [],
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("BriefingIndexView — ready カード描画", () => {
  it("ready が N 件のとき N 個の headline と /briefing/<slug> リンクが描画される", () => {
    const cards: ReadyBriefingCard[] = [
      makeReadyCard({
        category: { slug: "ai", name: "AI" },
        headline: "AI 動向レポート",
      }),
      makeReadyCard({
        category: { slug: "bio", name: "バイオ" },
        headline: "バイオ最新情報",
      }),
      makeReadyCard({
        category: { slug: "space", name: "スペース" },
        headline: "宇宙開発ニュース",
      }),
    ];
    render(
      <BriefingIndexView
        data={makeViewModel({ ready: cards, totalArticles: 30 })}
      />,
    );

    expect(screen.getByText("AI 動向レポート")).toBeInTheDocument();
    expect(screen.getByText("バイオ最新情報")).toBeInTheDocument();
    expect(screen.getByText("宇宙開発ニュース")).toBeInTheDocument();

    // 各カードは対応する slug へのリンクを持つ
    const links = screen.getAllByRole("link");
    const hrefs = links.map((l) => l.getAttribute("href"));
    expect(hrefs).toContain("/briefing/ai");
    expect(hrefs).toContain("/briefing/bio");
    expect(hrefs).toContain("/briefing/space");
  });

  it("ready が空のとき BriefingBandCard は 1 つも描画されない", () => {
    const pending: PendingCategory[] = [
      { slug: "ai", name: "AI" },
      { slug: "bio", name: "バイオ" },
    ];
    render(<BriefingIndexView data={makeViewModel({ ready: [], pending })} />);

    // リンク形式のカードは存在しない
    const links = screen.queryAllByRole("link");
    expect(links).toHaveLength(0);
  });
});

describe("BriefingIndexView — pending セクション", () => {
  it("pending があるとき「準備中」チップが表示される", () => {
    const pending: PendingCategory[] = [
      { slug: "space", name: "スペース" },
      { slug: "energy", name: "エネルギー" },
    ];
    render(<BriefingIndexView data={makeViewModel({ pending })} />);

    expect(screen.getByText("準備中")).toBeInTheDocument();
    expect(screen.getByText("スペース")).toBeInTheDocument();
    expect(screen.getByText("エネルギー")).toBeInTheDocument();
  });

  it("pending が空のとき「準備中」は表示されない", () => {
    const cards = [makeReadyCard()];
    render(
      <BriefingIndexView
        data={makeViewModel({ ready: cards, pending: [], totalArticles: 10 })}
      />,
    );

    expect(screen.queryByText("準備中")).not.toBeInTheDocument();
  });
});

describe("BriefingIndexView — 全カテゴリ pending 状態 (ready 空)", () => {
  it("ready 空 + pending あり のとき masthead と pending セクションが表示される", () => {
    const pending: PendingCategory[] = [
      { slug: "ai", name: "AI" },
      { slug: "bio", name: "バイオ" },
      { slug: "space", name: "スペース" },
    ];
    render(
      <BriefingIndexView
        data={makeViewModel({
          ready: [],
          pending,
          totalArticles: 0,
        })}
      />,
    );

    // masthead タイトルが表示される
    expect(
      screen.getByRole("heading", { level: 1, name: "今週のブリーフィング" }),
    ).toBeInTheDocument();

    // pending セクションが表示される
    expect(screen.getByText("準備中")).toBeInTheDocument();
    expect(screen.getByText("AI")).toBeInTheDocument();
    expect(screen.getByText("バイオ")).toBeInTheDocument();
    expect(screen.getByText("スペース")).toBeInTheDocument();

    // カードリンクは存在しない
    expect(screen.queryAllByRole("link")).toHaveLength(0);
  });
});

describe("BriefingIndexView — masthead 表示", () => {
  it("totalArticles が masthead に表示される", () => {
    render(<BriefingIndexView data={makeViewModel({ totalArticles: 88 })} />);
    expect(screen.getByText(/88/)).toBeInTheDocument();
  });
});
