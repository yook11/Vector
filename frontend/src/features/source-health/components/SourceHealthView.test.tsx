import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { formatDate } from "@/lib/date";
import type { SourceHealthItem, SourceHealthResponse } from "@/types/types.gen";
import { SourceHealthView } from "./SourceHealthView";

function makeItem(overrides: Partial<SourceHealthItem> = {}): SourceHealthItem {
  return {
    sourceId: 1,
    sourceName: "Alpha",
    sourceType: "rss",
    isActive: true,
    analyzableRate: 80,
    analyzableCount: 8,
    processedArticleCount: 10,
    incompleteCount: 2,
    failureReasons: [],
    lastSucceededAt: "2026-06-03T01:00:00Z",
    ...overrides,
  };
}

// 健全行 (active, 全指標あり, failure 5 件) と空行 (inactive, 全 0/null) を混ぜ、
// View の各分岐 (rate null/値, processed 0/値, incomplete 0/値, active/inactive,
// lastSucceeded null/値, failureReasons 空/非空) を両側で踏む。
const ACTIVE = makeItem({
  sourceId: 1,
  sourceName: "Alpha",
  sourceType: "rss",
  isActive: true,
  failureReasons: [
    { outcomeCode: "fetch_timeout", count: 5 },
    { outcomeCode: "scrape_not_html", count: 3 },
    { outcomeCode: "scrape_parser_gave_up", count: 2 },
    { outcomeCode: "fetch_network", count: 1 },
    { outcomeCode: "url_too_long", count: 1 },
  ],
});
const BLANK = makeItem({
  sourceId: 2,
  sourceName: "Bravo",
  sourceType: "api",
  isActive: false,
  analyzableRate: null,
  analyzableCount: 0,
  processedArticleCount: 0,
  incompleteCount: 0,
  failureReasons: [],
  lastSucceededAt: null,
});

const sample: SourceHealthResponse = {
  windowHours: 24,
  observedAt: "2026-06-03T02:00:00Z",
  items: [ACTIVE, BLANK],
};

function rowAt(index: number): HTMLElement {
  const row = screen.getAllByRole("row")[index];
  if (!row) throw new Error(`row[${index}] not found`);
  return row;
}

function cellAt(row: HTMLElement, index: number): HTMLElement {
  const cell = within(row).getAllByRole("cell")[index];
  if (!cell) throw new Error(`cell[${index}] not found`);
  return cell;
}

describe("SourceHealthView", () => {
  it("6 列のヘッダを持つ", () => {
    render(<SourceHealthView data={sample} />);
    expect(screen.getAllByRole("columnheader")).toHaveLength(6);
  });

  it("全 items を backend の配列順 (inactive も) そのまま行表示する", () => {
    render(<SourceHealthView data={sample} />);
    const rows = screen.getAllByRole("row");
    expect(rows).toHaveLength(1 + sample.items.length);
    expect(cellAt(rowAt(1), 0)).toHaveTextContent("Alpha");
    expect(cellAt(rowAt(2), 0)).toHaveTextContent("Bravo");
  });

  it("source type / active・inactive バッジを表示する", () => {
    render(<SourceHealthView data={sample} />);
    expect(within(rowAt(1)).getByText("rss")).toBeInTheDocument();
    expect(within(rowAt(1)).getByText("active")).toBeInTheDocument();
    expect(within(rowAt(2)).getByText("api")).toBeInTheDocument();
    expect(within(rowAt(2)).getByText("inactive")).toBeInTheDocument();
  });

  it("analyzableRate を数値は '%'、null は '-' で表示する", () => {
    render(<SourceHealthView data={sample} />);
    expect(cellAt(rowAt(1), 1)).toHaveTextContent("80%");
    expect(cellAt(rowAt(2), 1)).toHaveTextContent("-");
  });

  it("Analyzable 列に count / processed を表示する", () => {
    render(<SourceHealthView data={sample} />);
    expect(cellAt(rowAt(1), 2)).toHaveTextContent("8 / 10");
    expect(cellAt(rowAt(2), 2)).toHaveTextContent("0 / 0");
  });

  it("incompleteCount の 0 のみ淡色 (muted) にする", () => {
    render(<SourceHealthView data={sample} />);
    expect(cellAt(rowAt(2), 3)).toHaveClass("text-muted-foreground");
    expect(cellAt(rowAt(1), 3)).not.toHaveClass("text-muted-foreground");
  });

  it("failure reasons を省略せず全件 (5 件) 縦積み表示する", () => {
    render(<SourceHealthView data={sample} />);
    const cell = cellAt(rowAt(1), 4);
    expect(within(cell).getAllByRole("listitem")).toHaveLength(5);
    // backend のソート順 (count 降順) をそのまま描く。
    expect(within(cell).getByText("fetch_timeout")).toBeInTheDocument();
    expect(within(cell).getByText("url_too_long")).toBeInTheDocument();
  });

  it("failure reasons が空の source は '-' を表示する", () => {
    render(<SourceHealthView data={sample} />);
    expect(cellAt(rowAt(2), 4)).toHaveTextContent("-");
  });

  it("lastSucceededAt を datetime / null は '-' で表示する", () => {
    render(<SourceHealthView data={sample} />);
    expect(cellAt(rowAt(1), 5)).toHaveTextContent(
      formatDate(ACTIVE.lastSucceededAt, { withTime: true }),
    );
    expect(cellAt(rowAt(2), 5)).toHaveTextContent("-");
  });

  it("window を label・observedAt をキャプションに表示する", () => {
    render(<SourceHealthView data={sample} />);
    expect(
      screen.getByText(
        `24h window · observed ${formatDate(sample.observedAt, { withTime: true })}`,
      ),
    ).toBeInTheDocument();
  });

  it("7d (windowHours 168) を '168h' でなく '7d window' と表示する", () => {
    render(<SourceHealthView data={{ ...sample, windowHours: 168 }} />);
    expect(
      screen.getByText(
        `7d window · observed ${formatDate(sample.observedAt, { withTime: true })}`,
      ),
    ).toBeInTheDocument();
  });

  it("items が空のときは 'No sources.' を表示する", () => {
    const empty: SourceHealthResponse = {
      windowHours: 24,
      observedAt: "2026-06-03T02:00:00Z",
      items: [],
    };
    render(<SourceHealthView data={empty} />);
    expect(screen.getByText("No sources.")).toBeInTheDocument();
    // header のみ (data 行なし)。
    expect(screen.getAllByRole("row")).toHaveLength(2);
  });
});
