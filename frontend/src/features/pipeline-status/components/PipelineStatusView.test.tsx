import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { formatDate } from "@/lib/date";
import type {
  PipelineHealthResponse,
  PipelineStageHealth,
  Stage,
} from "@/types/types.gen";
import { PipelineStatusView } from "./PipelineStatusView";

// backend が返す Stage enum の定義順。View はこの順序を保ったまま行表示する。
const STAGES: Stage[] = [
  "dispatch",
  "acquisition",
  "completion",
  "curation",
  "assessment",
  "embedding",
  "backfill_curate",
  "backfill_assess",
  "backfill_embed",
  "briefing",
  "trend_discovery",
];

function makeStage(
  stage: Stage,
  overrides: Partial<PipelineStageHealth> = {},
): PipelineStageHealth {
  return {
    stage,
    succeededEventCount24h: 10,
    failedEventCount24h: 0,
    queueCount: 0,
    oldestQueueAgeSeconds: null,
    backfillTargetCount: 0,
    oldestBackfillTargetAgeSeconds: null,
    lastSucceededAt: "2026-06-03T01:00:00Z",
    ...overrides,
  };
}

// summary の各値と stage の各値が getByText で衝突しないよう数値を散らす。
const sample: PipelineHealthResponse = {
  summary: {
    failedEventCount24h: 2,
    backfillTargetTotal: 9,
    oldestBackfillTargetAgeSeconds: 4320, // → "1h 12m"
    completionQueueCount: 5,
    oldestCompletionQueueAgeSeconds: 60, // → "1m"
    observedAt: "2026-06-03T02:00:00Z",
    eventWindowStart: "2026-06-02T02:00:00Z",
  },
  stages: STAGES.map((s) => {
    if (s === "completion") {
      return makeStage(s, { queueCount: 4, oldestQueueAgeSeconds: 7320 }); // → "2h 2m"
    }
    if (s === "curation") {
      return makeStage(s, {
        backfillTargetCount: 7,
        oldestBackfillTargetAgeSeconds: 10800,
        lastSucceededAt: null,
      });
    }
    return makeStage(s);
  }),
};

// noUncheckedIndexedAccess 下で行・セルを型安全に取り出す helper。
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

describe("PipelineStatusView", () => {
  it("summary の 7 項目を表示する", () => {
    render(<PipelineStatusView data={sample} />);
    expect(screen.getByText("2")).toBeInTheDocument(); // failedEventCount24h
    expect(screen.getByText("9")).toBeInTheDocument(); // backfillTargetTotal
    expect(screen.getByText("1h 12m")).toBeInTheDocument(); // oldestBackfillTargetAgeSeconds
    expect(screen.getByText("5")).toBeInTheDocument(); // completionQueueCount
    expect(screen.getByText("1m")).toBeInTheDocument(); // oldestCompletionQueueAgeSeconds
    expect(
      screen.getByText(
        formatDate(sample.summary.observedAt, { withTime: true }),
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        formatDate(sample.summary.eventWindowStart, { withTime: true }),
      ),
    ).toBeInTheDocument();
  });

  it("全 11 stage を backend の配列順そのままで行表示する", () => {
    render(<PipelineStatusView data={sample} />);
    const rows = screen.getAllByRole("row");
    expect(rows).toHaveLength(1 + STAGES.length); // header + 11
    const stageNames = rows.slice(1).map((row) => cellAt(row, 0).textContent);
    expect(stageNames).toEqual(STAGES);
  });

  it("queue/backfill が 0/null の stage でも行を隠さない", () => {
    render(<PipelineStatusView data={sample} />);
    // dispatch は queue=0/null・backfill=0/null だが行は維持される。
    expect(screen.getAllByRole("row")).toHaveLength(1 + STAGES.length);
    expect(cellAt(rowAt(1), 0)).toHaveTextContent("dispatch");
  });

  it("null の age セルは '-' を表示する", () => {
    render(<PipelineStatusView data={sample} />);
    const row = rowAt(1); // dispatch
    // 0:stage 1:succeeded 2:failed 3:last 4:queue 5:queueAge 6:backfill 7:backfillAge
    expect(cellAt(row, 5)).toHaveTextContent("-");
    expect(cellAt(row, 7)).toHaveTextContent("-");
  });

  it("age 秒数を 'Nh Nm' 形式に整形する", () => {
    render(<PipelineStatusView data={sample} />);
    // rows[3] = completion (oldestQueueAgeSeconds 7320 → "2h 2m")
    expect(cellAt(rowAt(3), 5)).toHaveTextContent("2h 2m");
  });

  it("lastSucceededAt が null の stage は '-' を表示する", () => {
    render(<PipelineStatusView data={sample} />);
    // rows[4] = curation (lastSucceededAt null)
    expect(cellAt(rowAt(4), 3)).toHaveTextContent("-");
  });

  it("8 列のヘッダを持つ", () => {
    render(<PipelineStatusView data={sample} />);
    expect(screen.getAllByRole("columnheader")).toHaveLength(8);
  });

  it("summary の 0/null 値は淡色 (muted) で表示する", () => {
    const zeroSample: PipelineHealthResponse = {
      summary: {
        failedEventCount24h: 0,
        backfillTargetTotal: 0,
        oldestBackfillTargetAgeSeconds: null,
        completionQueueCount: 0,
        oldestCompletionQueueAgeSeconds: null,
        observedAt: "2026-06-03T02:00:00Z",
        eventWindowStart: "2026-06-02T02:00:00Z",
      },
      stages: [],
    };
    render(<PipelineStatusView data={zeroSample} />);
    // "Backfill targets" の値 0 は意味を持たないので淡色になる。dt に限定して
    // stages table の同名 column header (th) との衝突を避ける。
    const container = screen.getByText("Backfill targets", {
      selector: "dt",
    }).parentElement;
    if (!container) throw new Error("summary item container not found");
    expect(within(container).getByText("0")).toHaveClass(
      "text-muted-foreground",
    );
  });
});
