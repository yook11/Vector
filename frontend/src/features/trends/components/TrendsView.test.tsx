import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type {
  CategoryTrends,
  CoMention,
  MentionTrend,
  Trends,
} from "@/types/types.gen";

function makeCoMention(
  name: string,
  overrides: Partial<CoMention> = {},
): CoMention {
  return {
    name,
    type: "company",
    sharedArticleCount: 3,
    ...overrides,
  };
}

/** 既定では記事数の列だけに出る (記事数1位、伸び率の順位なし)。 */
function makeTrend(
  name: string,
  overrides: {
    type?: MentionTrend["type"];
    count?: number;
    previousWeekCount?: number;
    articleRank?: number;
    rate?: number;
    growthRank?: number | null;
    keyPoints?: string[];
    mentionedWith?: CoMention[];
  } = {},
): MentionTrend {
  return {
    name,
    type: overrides.type ?? "company",
    articleVolume: {
      count: overrides.count ?? 20,
      previousWeekCount: overrides.previousWeekCount ?? 10,
      rank: overrides.articleRank ?? 1,
    },
    growth: {
      rate: overrides.rate ?? 1.0,
      rank: overrides.growthRank ?? null,
    },
    keyPoints: overrides.keyPoints ?? [],
    mentionedWith: overrides.mentionedWith ?? [],
  };
}

function makeCategory(
  slug: string,
  name: string,
  mentionTrends: MentionTrend[] = [],
): CategoryTrends {
  return { category: { slug, name }, mentionTrends };
}

/** 最小限の Trends サンプルデータ。各テストが必要なフィールドだけ上書きする。 */
function makeTrends(overrides: Partial<Trends> = {}): Trends {
  return {
    week: { start: "2026-04-26", end: "2026-05-02" },
    previousWeek: { start: "2026-04-19", end: "2026-04-25" },
    generatedAt: "2026-05-03T06:00:00Z",
    analyzedArticleCount: 158,
    categoryTrends: [],
    ...overrides,
  };
}

/** カテゴリ内の、行がある列ごとの行の文字列。列は記事数・伸び率の順に並ぶ。 */
function listedRows(categoryName: string): string[][] {
  const section = screen.getByRole("region", { name: categoryName });
  return within(section)
    .getAllByRole("list")
    .map((list) =>
      within(list)
        .getAllByRole("button")
        .map((button) => button.textContent ?? ""),
    );
}

import { TrendsView } from "./TrendsView";

describe("TrendsView — マストヘッド", () => {
  it("analyzedArticleCount が表示される", () => {
    render(<TrendsView data={makeTrends({ analyzedArticleCount: 158 })} />);
    expect(screen.getByText(/158 件の記事から集計/)).toBeInTheDocument();
  });

  it("週は初日から最終日までで表示される", () => {
    render(
      <TrendsView
        data={makeTrends({ week: { start: "2026-04-26", end: "2026-05-02" } })}
      />,
    );
    expect(
      screen.getByText(/2026年4月26日 – 2026年5月2日/),
    ).toBeInTheDocument();
  });

  it("最終更新(generatedAt)が表示される", () => {
    render(
      <TrendsView data={makeTrends({ generatedAt: "2026-05-03T06:00:00Z" })} />,
    );
    expect(screen.getByText(/最終更新/)).toBeInTheDocument();
  });
});

describe("TrendsView — カテゴリ", () => {
  it("複数カテゴリの name が見出しに出る", () => {
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI & 機械学習"),
            makeCategory("computing", "コンピューティング"),
          ],
        })}
      />,
    );
    expect(screen.getByText("AI & 機械学習")).toBeInTheDocument();
    expect(screen.getByText("コンピューティング")).toBeInTheDocument();
  });

  it("カテゴリ eyebrow コード(slug → A.I. 等)が出る", () => {
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI"),
            makeCategory("computing", "Computing"),
          ],
        })}
      />,
    );
    expect(screen.getByText("A.I.")).toBeInTheDocument();
    expect(screen.getByText("COMPUTE")).toBeInTheDocument();
  });
});

describe("TrendsView — 2つの列", () => {
  it("言及数上位・急上昇ワードのラベルと、記事数順の説明が各カテゴリに出る", () => {
    render(
      <TrendsView
        data={makeTrends({ categoryTrends: [makeCategory("ai", "AI")] })}
      />,
    );
    expect(screen.getByText("言及数上位")).toBeInTheDocument();
    expect(screen.getByText("急上昇ワード")).toBeInTheDocument();
    expect(screen.getByText("記事数順")).toBeInTheDocument();
  });

  it("記事数の列は記事数の順位で並び、同じ順位は名前順になる", () => {
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", [
              makeTrend("Gamma", { articleRank: 2 }),
              makeTrend("Beta", { articleRank: 2 }),
              makeTrend("Alpha", { articleRank: 1 }),
            ]),
          ],
        })}
      />,
    );
    // 伸び率の順位を持たないので、行があるのは記事数の列だけ。
    const [count] = listedRows("AI");
    expect(count).toEqual([
      expect.stringMatching(/^1Alpha/),
      expect.stringMatching(/^2Beta/),
      expect.stringMatching(/^2Gamma/),
    ]);
  });

  it("伸び率の列は伸び率の順位で並び、順位のない名前は出ない", () => {
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", [
              makeTrend("Steady", { articleRank: 1, growthRank: null }),
              makeTrend("Fast2", { articleRank: 3, growthRank: 2 }),
              makeTrend("Fast1", { articleRank: 2, growthRank: 1 }),
            ]),
          ],
        })}
      />,
    );
    const [, growth] = listedRows("AI");
    expect(growth).toEqual([
      expect.stringMatching(/^1Fast1/),
      expect.stringMatching(/^2Fast2/),
    ]);
  });

  it("どちらの列も5位以内の名前だけを出す", () => {
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", [
              makeTrend("Top", { articleRank: 1, growthRank: 6 }),
              makeTrend("Rising", { articleRank: 6, growthRank: 1 }),
            ]),
          ],
        })}
      />,
    );
    expect(listedRows("AI")).toEqual([
      [expect.stringMatching(/^1Top/)],
      [expect.stringMatching(/^1Rising/)],
    ]);
  });

  it("両方の列で上位の名前は、両方の列に出る", () => {
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", [
              makeTrend("SharedEntity", { articleRank: 1, growthRank: 1 }),
            ]),
          ],
        })}
      />,
    );
    expect(screen.getAllByText("SharedEntity")).toHaveLength(2);
  });

  it("列に出す名前がなければ「該当するワードはありません」", () => {
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", [
              makeTrend("CountOnly", { articleRank: 1, growthRank: null }),
            ]),
          ],
        })}
      />,
    );
    expect(screen.getByText("該当するワードはありません")).toBeInTheDocument();
  });
});

describe("TrendsView — 行の表示内容", () => {
  it("name・種別バッジ日本語・週と前週の記事数が出る", () => {
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", [
              makeTrend("OpenAI", {
                type: "company",
                count: 42,
                previousWeekCount: 17,
                rate: 1.47,
              }),
            ]),
          ],
        })}
      />,
    );
    expect(screen.getByText("OpenAI")).toBeInTheDocument();
    expect(screen.getAllByText("企業").length).toBeGreaterThan(0);
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.getByText(/前週\s*17/)).toBeInTheDocument();
  });

  it("growth.rate は data の値をそのまま整形する(記事数から再計算しない)", () => {
    // 30 件・前週 5 件から単純計算すると +500% だが、rate=9.99 なので "+999%"
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", [
              makeTrend("TestCorp", {
                count: 30,
                previousWeekCount: 5,
                rate: 9.99,
              }),
            ]),
          ],
        })}
      />,
    );
    expect(screen.getByText("+999%")).toBeInTheDocument();
    expect(screen.queryByText("+500%")).not.toBeInTheDocument();
  });

  it("前週0件の名前に「新登場」が出る", () => {
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", [
              makeTrend("NewComer", {
                previousWeekCount: 0,
                articleRank: 6,
                growthRank: 1,
              }),
            ]),
          ],
        })}
      />,
    );
    expect(screen.getByText("新登場")).toBeInTheDocument();
  });

  it("前週が1件以上の名前に「新登場」は出ない", () => {
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", [
              makeTrend("OldComer", {
                previousWeekCount: 5,
                articleRank: 6,
                growthRank: 1,
              }),
            ]),
          ],
        })}
      />,
    );
    expect(screen.queryByText("新登場")).not.toBeInTheDocument();
  });

  it("growth.rate<0 の名前で U+2212 付き負の伸び率が出る", () => {
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", [
              makeTrend("Declining", { rate: -0.13, previousWeekCount: 10 }),
            ]),
          ],
        })}
      />,
    );
    // U+2212 MINUS SIGN
    expect(screen.getByText("−13%")).toBeInTheDocument();
  });
});

describe("TrendsView — 展開パネル", () => {
  it("初期状態で keyPoints / mentionedWith は非表示", () => {
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", [
              makeTrend("NVIDIA", {
                keyPoints: ["GPU 需要が急増"],
                mentionedWith: [makeCoMention("AMD")],
              }),
            ]),
          ],
        })}
      />,
    );
    expect(screen.queryByText("GPU 需要が急増")).not.toBeInTheDocument();
    expect(screen.queryByText("AMD")).not.toBeInTheDocument();
  });

  it("行をクリックすると keyPoints と mentionedWith が表示される", async () => {
    const user = userEvent.setup();
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", [
              makeTrend("NVIDIA", {
                keyPoints: ["GPU 需要が急増", "データセンター向け好調"],
                mentionedWith: [
                  makeCoMention("AMD", { sharedArticleCount: 7 }),
                ],
              }),
            ]),
          ],
        })}
      />,
    );
    await user.click(screen.getByRole("button", { expanded: false }));
    expect(screen.getByText("GPU 需要が急増")).toBeInTheDocument();
    expect(screen.getByText("データセンター向け好調")).toBeInTheDocument();
    expect(screen.getByText("AMD")).toBeInTheDocument();
    expect(screen.getByText("7件")).toBeInTheDocument();
  });

  it("展開後に再クリックすると閉じる", async () => {
    const user = userEvent.setup();
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", [
              makeTrend("NVIDIA", { keyPoints: ["要点A"] }),
            ]),
          ],
        })}
      />,
    );
    await user.click(screen.getByRole("button", { expanded: false }));
    expect(screen.getByText("要点A")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { expanded: true }));
    expect(screen.queryByText("要点A")).not.toBeInTheDocument();
  });

  it("keyPoints 空の名前を展開すると「要点は登録されていません」", async () => {
    const user = userEvent.setup();
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", [
              makeTrend("NoPoints", {
                keyPoints: [],
                mentionedWith: [makeCoMention("Other")],
              }),
            ]),
          ],
        })}
      />,
    );
    await user.click(screen.getByRole("button", { expanded: false }));
    expect(screen.getByText("要点は登録されていません")).toBeInTheDocument();
  });

  it("mentionedWith 空の名前を展開すると「共起した固有名はありません」", async () => {
    const user = userEvent.setup();
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", [
              makeTrend("NoCoMention", { keyPoints: ["何か要点"] }),
            ]),
          ],
        })}
      />,
    );
    await user.click(screen.getByRole("button", { expanded: false }));
    expect(screen.getByText("共起した固有名はありません")).toBeInTheDocument();
  });
});
