import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { formatPaperDate, getCategoryKicker } from "@/components/paper";
import type { ReadyBriefingCard } from "../page-models/briefing-list";
import { BriefingBandCard } from "./BriefingBandCard";

function makeCard(
  overrides: Partial<ReadyBriefingCard> = {},
): ReadyBriefingCard {
  return {
    category: { slug: "ai", name: "AI" },
    weekStart: "2026-06-02",
    headline: "AI 技術の最前線",
    // summary は「週」を含まないようにする (stale-week ラベルのテストが誤検知しないため)
    summary: "AI分野の最新動向レポート",
    inputArticleCount: 12,
    ...overrides,
  };
}

describe("BriefingBandCard — コンテンツ表示", () => {
  it("headline と summary が表示される", () => {
    render(
      <BriefingBandCard
        card={makeCard({
          headline: "量子コンピューティング速報",
          summary: "量子技術の最新動向",
        })}
        currentWeekStart="2026-06-02"
      />,
    );
    expect(screen.getByText("量子コンピューティング速報")).toBeInTheDocument();
    expect(screen.getByText("量子技術の最新動向")).toBeInTheDocument();
  });

  it("inputArticleCount と「件」単位が表示される", () => {
    render(
      <BriefingBandCard
        card={makeCard({ inputArticleCount: 42 })}
        currentWeekStart="2026-06-02"
      />,
    );
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.getByText(/件/)).toBeInTheDocument();
  });

  it("category.name が表示される", () => {
    render(
      <BriefingBandCard
        card={makeCard({
          category: { slug: "security", name: "セキュリティ" },
        })}
        currentWeekStart="2026-06-02"
      />,
    );
    expect(screen.getByText("セキュリティ")).toBeInTheDocument();
  });

  it("slug から導出したカテゴリコードが表示される (ai → A.I.)", () => {
    // getCategoryKicker を使って期待値を導出し、production logic を複製しない
    const expectedCode = getCategoryKicker("ai").code;
    render(
      <BriefingBandCard
        card={makeCard({ category: { slug: "ai", name: "AI" } })}
        currentWeekStart="2026-06-02"
      />,
    );
    expect(screen.getByText(expectedCode)).toBeInTheDocument();
    expect(expectedCode).toBe("A.I.");
  });

  it("slug から導出したカテゴリコードが表示される (security → SECURITY)", () => {
    const expectedCode = getCategoryKicker("security").code;
    render(
      <BriefingBandCard
        card={makeCard({
          category: { slug: "security", name: "セキュリティ" },
        })}
        currentWeekStart="2026-06-02"
      />,
    );
    expect(screen.getByText(expectedCode)).toBeInTheDocument();
  });
});

describe("BriefingBandCard — リンク", () => {
  it("カードは /briefing/<slug> へのリンクになっている", () => {
    render(
      <BriefingBandCard
        card={makeCard({
          category: { slug: "computing", name: "コンピューティング" },
        })}
        currentWeekStart="2026-06-02"
      />,
    );
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", "/briefing/computing");
  });
});

describe("BriefingBandCard — stale-week ラベル", () => {
  it("card.weekStart が currentWeekStart より前 (古い週) のとき週ラベルが表示される", () => {
    // 古い週のカード: card.weekStart は 1 週前
    const staleWeekStart = "2026-05-26";
    render(
      <BriefingBandCard
        card={makeCard({ weekStart: staleWeekStart })}
        currentWeekStart="2026-06-02"
      />,
    );
    // 週ラベルは formatPaperDate(card.weekStart) + " 週" のテキストノード
    // formatPaperDate で期待値を導出し hardcode を避ける
    const expectedLabel = `${formatPaperDate(staleWeekStart)} 週`;
    expect(screen.getByText(expectedLabel)).toBeInTheDocument();
  });

  it("card.weekStart === currentWeekStart のとき週ラベルは表示されない", () => {
    // 今週のカード: weekStart が currentWeekStart と一致
    const currentWeek = "2026-06-02";
    render(
      <BriefingBandCard
        card={makeCard({ weekStart: currentWeek })}
        currentWeekStart={currentWeek}
      />,
    );
    // stale-week span の内容は formatPaperDate + " 週" 形式
    const labelThatShouldNotExist = `${formatPaperDate(currentWeek)} 週`;
    expect(screen.queryByText(labelThatShouldNotExist)).not.toBeInTheDocument();
  });

  it("card.weekStart が currentWeekStart より後 (新しい週) のとき週ラベルは表示されない", () => {
    // 「古い週のみ」表示する契約。未来週 (CLI --week 等) を stale 扱いしない
    const futureWeek = "2026-06-09";
    render(
      <BriefingBandCard
        card={makeCard({ weekStart: futureWeek })}
        currentWeekStart="2026-06-02"
      />,
    );
    const labelThatShouldNotExist = `${formatPaperDate(futureWeek)} 週`;
    expect(screen.queryByText(labelThatShouldNotExist)).not.toBeInTheDocument();
  });
});
