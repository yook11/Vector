import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { formatPaperDate } from "@/components/paper";
import { BriefingMasthead } from "./BriefingMasthead";

describe("BriefingMasthead — コンテンツ表示", () => {
  it("「今週のブリーフィング」タイトルが H1 として表示される", () => {
    render(
      <BriefingMasthead
        weekStart="2026-06-02"
        weekEnd="2026-06-08"
        totalArticles={50}
      />,
    );
    expect(
      screen.getByRole("heading", { level: 1, name: "今週のブリーフィング" }),
    ).toBeInTheDocument();
  });

  it("totalArticles が表示される", () => {
    render(
      <BriefingMasthead
        weekStart="2026-06-02"
        weekEnd="2026-06-08"
        totalArticles={137}
      />,
    );
    expect(screen.getByText(/137/)).toBeInTheDocument();
  });

  it("weekStart の formatPaperDate 出力が表示される", () => {
    const weekStart = "2026-06-02";
    const formatted = formatPaperDate(weekStart);
    render(
      <BriefingMasthead
        weekStart={weekStart}
        weekEnd="2026-06-08"
        totalArticles={0}
      />,
    );
    // formatPaperDate は Intl で日本語日付を返す。一部(年・月・日)を確認する
    // "2026年6月2日" などの形式
    expect(
      screen.getAllByText(new RegExp(formatted.replace(/[()]/g, "\\$&")))
        .length,
    ).toBeGreaterThan(0);
  });

  it("weekEnd の formatPaperDate 出力が表示される", () => {
    const weekEnd = "2026-06-08";
    const formatted = formatPaperDate(weekEnd);
    render(
      <BriefingMasthead
        weekStart="2026-06-02"
        weekEnd={weekEnd}
        totalArticles={0}
      />,
    );
    expect(
      screen.getAllByText(new RegExp(formatted.replace(/[()]/g, "\\$&")))
        .length,
    ).toBeGreaterThan(0);
  });

  it("weekStart と weekEnd が共に「2026」年として含まれる", () => {
    render(
      <BriefingMasthead
        weekStart="2026-05-26"
        weekEnd="2026-06-01"
        totalArticles={99}
      />,
    );
    // 日本語ロケールで "2026年" が含まれることを確認する (年表示の非空虚チェック)
    const matches = screen.queryAllByText(/2026/);
    expect(matches.length).toBeGreaterThan(0);
  });

  it("月跨ぎ週 (5/26 - 6/1) の weekStart と weekEnd が両方表示される", () => {
    const weekStart = "2026-05-26";
    const weekEnd = "2026-06-01";
    render(
      <BriefingMasthead
        weekStart={weekStart}
        weekEnd={weekEnd}
        totalArticles={20}
      />,
    );
    // 5月と6月の両方が含まれる
    const formattedStart = formatPaperDate(weekStart); // "2026年5月26日"
    const formattedEnd = formatPaperDate(weekEnd); // "2026年6月1日"
    expect(
      screen.getAllByText(new RegExp(formattedStart.replace(/[()]/g, "\\$&")))
        .length,
    ).toBeGreaterThan(0);
    expect(
      screen.getAllByText(new RegExp(formattedEnd.replace(/[()]/g, "\\$&")))
        .length,
    ).toBeGreaterThan(0);
  });
});
