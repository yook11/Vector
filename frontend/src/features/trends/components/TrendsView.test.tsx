import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type {
  CategoryTrends,
  RankedMention,
  RelatedMention,
  Trends,
} from "@/types";

function makeRelated(
  name: string,
  overrides: Partial<RelatedMention> = {},
): RelatedMention {
  return {
    name,
    type: "company",
    sharedArticleCount: 3,
    ...overrides,
  };
}

function makeMention(
  name: string,
  overrides: Partial<RankedMention> = {},
): RankedMention {
  return {
    name,
    type: "company",
    appearanceCount: 20,
    previousAppearanceCount: 10,
    growthRate: 1.0,
    keyPoints: [],
    relatedMentions: [],
    ...overrides,
  };
}

function makeCategory(
  slug: string,
  name: string,
  overrides: Partial<CategoryTrends> = {},
): CategoryTrends {
  return {
    categoryId: Math.floor(Math.random() * 10000),
    categorySlug: slug,
    categoryName: name,
    mostMentioned: [],
    fastestGrowing: [],
    ...overrides,
  };
}

/** 最小限の Trends サンプルデータ。各テストが必要なフィールドだけ上書きする。 */
function makeTrends(overrides: Partial<Trends> = {}): Trends {
  return {
    state: "trends",
    windowStart: "2026-04-26",
    windowEnd: "2026-05-03",
    generatedAt: "2026-05-03T06:00:00Z",
    sourceAnalysisCount: 158,
    categoryTrends: [],
    ...overrides,
  };
}

import { TrendsView } from "./TrendsView";

describe("TrendsView — マストヘッド", () => {
  it("sourceAnalysisCount が表示される", () => {
    render(<TrendsView data={makeTrends({ sourceAnalysisCount: 158 })} />);
    expect(screen.getByText(/158/)).toBeInTheDocument();
  });

  it("windowStart / windowEnd 由来の期間が表示される", () => {
    render(
      <TrendsView
        data={makeTrends({
          windowStart: "2026-04-26",
          windowEnd: "2026-05-03",
        })}
      />,
    );
    // formatDate("2026-04-26") → 日本語日付文字列の一部が含まれる
    expect(screen.getByText(/2026/)).toBeInTheDocument();
  });

  it("最終更新(generatedAt)が表示される", () => {
    render(
      <TrendsView data={makeTrends({ generatedAt: "2026-05-03T06:00:00Z" })} />,
    );
    // "最終更新" ラベルが出る
    expect(screen.getByText(/最終更新/)).toBeInTheDocument();
  });
});

describe("TrendsView — カテゴリ", () => {
  it("複数カテゴリの categoryName が見出しに出る", () => {
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI & 機械学習", { categoryId: 1 }),
            makeCategory("computing", "コンピューティング", { categoryId: 2 }),
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
            makeCategory("ai", "AI", { categoryId: 1 }),
            makeCategory("computing", "Computing", { categoryId: 2 }),
          ],
        })}
      />,
    );
    expect(screen.getByText("A.I.")).toBeInTheDocument();
    expect(screen.getByText("COMPUTE")).toBeInTheDocument();
  });
});

describe("TrendsView — 2ランキングラベル", () => {
  it("言及数上位・急上昇ワード ラベルが各カテゴリに出る", () => {
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [makeCategory("ai", "AI", { categoryId: 1 })],
        })}
      />,
    );
    expect(screen.getByText("言及数上位")).toBeInTheDocument();
    expect(screen.getByText("急上昇ワード")).toBeInTheDocument();
  });

  it("mostMentioned の並び順がそのまま行順に反映される(1..N)", () => {
    const mentions = [
      makeMention("Alpha", { appearanceCount: 30 }),
      makeMention("Beta", { appearanceCount: 20 }),
      makeMention("Gamma", { appearanceCount: 10 }),
    ];
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", {
              categoryId: 1,
              mostMentioned: mentions,
            }),
          ],
        })}
      />,
    );
    // カテゴリ section を scope に順位ボタンを取得
    const section = screen.getByRole("region", { name: "AI" });
    const buttons = within(section).getAllByRole("button", {
      expanded: false,
    });
    // "言及数上位" 列の buttons は最初の 3 個
    const rankButtons = buttons.slice(0, 3);
    expect(rankButtons[0]).toHaveTextContent("Alpha");
    expect(rankButtons[1]).toHaveTextContent("Beta");
    expect(rankButtons[2]).toHaveTextContent("Gamma");
  });

  it("fastestGrowing の並び順がそのまま行順に反映される", () => {
    const mentions = [
      makeMention("Fast1", { growthRate: 5.0 }),
      makeMention("Fast2", { growthRate: 2.0 }),
    ];
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", {
              categoryId: 1,
              fastestGrowing: mentions,
            }),
          ],
        })}
      />,
    );
    const section = screen.getByRole("region", { name: "AI" });
    const buttons = within(section).getAllByRole("button");
    // fastestGrowing 列のみなので buttons[0], buttons[1] がそのまま
    expect(buttons[0]).toHaveTextContent("Fast1");
    expect(buttons[1]).toHaveTextContent("Fast2");
  });
});

describe("TrendsView — 行の表示内容", () => {
  it("mention の name・種別バッジ日本語・appearanceCount・previousAppearanceCount が出る", () => {
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", {
              categoryId: 1,
              mostMentioned: [
                makeMention("OpenAI", {
                  type: "company",
                  appearanceCount: 42,
                  previousAppearanceCount: 17,
                  growthRate: 1.47,
                }),
              ],
            }),
          ],
        })}
      />,
    );
    expect(screen.getByText("OpenAI")).toBeInTheDocument();
    // TypeBadge が type=company → "企業"
    expect(screen.getAllByText("企業").length).toBeGreaterThan(0);
    expect(screen.getByText("42")).toBeInTheDocument();
    // 前週表示("前週 17")
    expect(screen.getByText(/前週\s*17/)).toBeInTheDocument();
  });

  it("growthRate は data の値をそのまま整形する(prev/now から再計算しない)", () => {
    // appearanceCount=30, previousAppearanceCount=5 から単純計算すると +500%
    // だが growthRate=9.99 を渡すので "+999%" が表示されるべき
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", {
              categoryId: 1,
              mostMentioned: [
                makeMention("TestCorp", {
                  appearanceCount: 30,
                  previousAppearanceCount: 5,
                  growthRate: 9.99,
                }),
              ],
            }),
          ],
        })}
      />,
    );
    expect(screen.getByText("+999%")).toBeInTheDocument();
    expect(screen.queryByText("+500%")).not.toBeInTheDocument();
  });

  it("previousAppearanceCount===0 の mention に「新登場」が出る", () => {
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", {
              categoryId: 1,
              fastestGrowing: [
                makeMention("NewComer", { previousAppearanceCount: 0 }),
              ],
            }),
          ],
        })}
      />,
    );
    expect(screen.getByText("新登場")).toBeInTheDocument();
  });

  it("previousAppearanceCount>0 の mention に「新登場」は出ない", () => {
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", {
              categoryId: 1,
              fastestGrowing: [
                makeMention("OldComer", { previousAppearanceCount: 5 }),
              ],
            }),
          ],
        })}
      />,
    );
    expect(screen.queryByText("新登場")).not.toBeInTheDocument();
  });

  it("growthRate<0 の mention で U+2212 付き負の伸び率が出る", () => {
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", {
              categoryId: 1,
              mostMentioned: [
                makeMention("Declining", {
                  growthRate: -0.13,
                  previousAppearanceCount: 10,
                }),
              ],
            }),
          ],
        })}
      />,
    );
    // U+2212 MINUS SIGN
    expect(screen.getByText("−13%")).toBeInTheDocument();
  });
});

describe("TrendsView — 展開パネル", () => {
  it("初期状態で keyPoints / relatedMentions は非表示", () => {
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", {
              categoryId: 1,
              mostMentioned: [
                makeMention("NVIDIA", {
                  keyPoints: ["GPU 需要が急増"],
                  relatedMentions: [makeRelated("AMD")],
                }),
              ],
            }),
          ],
        })}
      />,
    );
    expect(screen.queryByText("GPU 需要が急増")).not.toBeInTheDocument();
    expect(screen.queryByText("AMD")).not.toBeInTheDocument();
  });

  it("行をクリックすると keyPoints と relatedMentions が表示される", async () => {
    const user = userEvent.setup();
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", {
              categoryId: 1,
              mostMentioned: [
                makeMention("NVIDIA", {
                  keyPoints: ["GPU 需要が急増", "データセンター向け好調"],
                  relatedMentions: [
                    makeRelated("AMD", { sharedArticleCount: 7 }),
                  ],
                }),
              ],
            }),
          ],
        })}
      />,
    );
    const button = screen.getByRole("button", { expanded: false });
    await user.click(button);
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
            makeCategory("ai", "AI", {
              categoryId: 1,
              mostMentioned: [
                makeMention("NVIDIA", {
                  keyPoints: ["要点A"],
                  relatedMentions: [],
                }),
              ],
            }),
          ],
        })}
      />,
    );
    const button = screen.getByRole("button", { expanded: false });
    await user.click(button);
    expect(screen.getByText("要点A")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { expanded: true }));
    expect(screen.queryByText("要点A")).not.toBeInTheDocument();
  });

  it("keyPoints 空の mention を展開すると「要点は登録されていません」", async () => {
    const user = userEvent.setup();
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", {
              categoryId: 1,
              mostMentioned: [
                makeMention("NoPoints", {
                  keyPoints: [],
                  relatedMentions: [makeRelated("Other")],
                }),
              ],
            }),
          ],
        })}
      />,
    );
    await user.click(screen.getByRole("button", { expanded: false }));
    expect(screen.getByText("要点は登録されていません")).toBeInTheDocument();
  });

  it("relatedMentions 空の mention を展開すると「共起した固有名はありません」", async () => {
    const user = userEvent.setup();
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", {
              categoryId: 1,
              mostMentioned: [
                makeMention("NoRelated", {
                  keyPoints: ["何か要点"],
                  relatedMentions: [],
                }),
              ],
            }),
          ],
        })}
      />,
    );
    await user.click(screen.getByRole("button", { expanded: false }));
    expect(screen.getByText("共起した固有名はありません")).toBeInTheDocument();
  });
});

describe("TrendsView — key 衝突なし", () => {
  it("同一 mention が mostMentioned と fastestGrowing 両方に載っても壊れず両方描画される", () => {
    const sharedMention = makeMention("SharedEntity", {
      type: "technology",
      growthRate: 2.0,
    });
    render(
      <TrendsView
        data={makeTrends({
          categoryTrends: [
            makeCategory("ai", "AI", {
              categoryId: 1,
              mostMentioned: [sharedMention],
              fastestGrowing: [sharedMention],
            }),
          ],
        })}
      />,
    );
    // 同一 name が両カラムに出るので 2 箇所
    expect(screen.getAllByText("SharedEntity")).toHaveLength(2);
  });
});
