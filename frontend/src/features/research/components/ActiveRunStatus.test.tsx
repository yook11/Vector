import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ResearchLiveActivity } from "../live/events";
import { ActiveRunStatus } from "./ActiveRunStatus";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ActiveRunStatus", () => {
  it.each([
    ["queued", null, "待機中"],
    ["running", null, "生成中"],
    ["running", "planning", "計画中"],
    ["running", "retrieving", "情報収集中"],
    ["running", "synthesizing", "回答作成中"],
  ] as const)("renders %s / %s as %s", (status, stage, text) => {
    render(<ActiveRunStatus status={status} stage={stage} activity={null} />);

    expect(screen.getByText(text)).toBeInTheDocument();
  });

  it.each([
    [
      "retrieving",
      { type: "internal_search.started", queryCount: 2 },
      "関連記事を検索中",
    ],
    [
      "retrieving",
      { type: "internal_search.completed", hitCount: 8 },
      "関連記事8件を確認",
    ],
    [
      "retrieving",
      {
        type: "external_search.queries_generated",
        taskIndex: 0,
        queries: ["NVIDIA AI", "半導体需要"],
      },
      "“NVIDIA AI” など2件を検索中",
    ],
    [
      "retrieving",
      {
        type: "external_search.candidates_fetched",
        taskIndex: 1,
        candidateCount: 12,
      },
      "候補12件を取得",
    ],
    [
      "retrieving",
      {
        type: "external_search.evidence_selected",
        taskIndex: 1,
        evidenceCount: 4,
      },
      "根拠4件を選別",
    ],
    [
      "planning",
      {
        type: "question.resolved",
        standaloneQuestion: "NVIDIAの発表は株価へどう影響する？",
      },
      "“NVIDIAの発表は株価へどう影響する？”について調査中",
    ],
  ] satisfies ReadonlyArray<
    readonly ["planning" | "retrieving", ResearchLiveActivity, string]
  >)("renders the known $1 activity", (stage, activity, text) => {
    render(
      <ActiveRunStatus status="running" stage={stage} activity={activity} />,
    );

    expect(screen.getByText(text)).toBeInTheDocument();
  });

  it("leaves live notification ownership to the workspace announcer", () => {
    const { container } = render(
      <ActiveRunStatus
        status="running"
        stage="retrieving"
        activity={{
          type: "external_search.candidates_fetched",
          taskIndex: 0,
          candidateCount: 8,
        }}
      />,
    );

    expect(screen.getByText("情報収集中")).toBeInTheDocument();
    expect(screen.getByText("候補8件を取得")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(container.querySelector("[aria-live]")).toBeNull();
  });

  it("keeps the stage on one line and long activity to two breakable lines", () => {
    render(
      <ActiveRunStatus
        status="running"
        stage="retrieving"
        activity={{
          type: "external_search.queries_generated",
          taskIndex: 0,
          queries: [
            "VeryLongSearchQueryWithoutNaturalWhitespaceForOverflowVerification",
          ],
        }}
      />,
    );

    expect(screen.getByText("情報収集中")).toHaveClass("whitespace-nowrap");
    expect(
      screen.getByText(
        "“VeryLongSearchQueryWithoutNaturalWhitespaceForOverflowVerification” を検索中",
      ),
    ).toHaveClass("line-clamp-2", "break-words", "[overflow-wrap:anywhere]");
  });

  it("hides activity when it does not describe the current stage", () => {
    render(
      <ActiveRunStatus
        status="running"
        stage="synthesizing"
        activity={{
          type: "external_search.candidates_fetched",
          taskIndex: 0,
          candidateCount: 8,
        }}
      />,
    );

    expect(screen.queryByText("候補8件を取得")).not.toBeInTheDocument();
  });

  it("marks the spinner decorative and disables its animation for reduced motion", () => {
    render(
      <ActiveRunStatus status="running" stage="planning" activity={null} />,
    );

    const spinner = document.querySelector('[aria-hidden="true"].animate-spin');
    expect(spinner).not.toBeNull();
    expect(spinner).toHaveClass("animate-spin");
    expect(spinner).toHaveClass("motion-reduce:animate-none");
  });

  it("does not own fetch, EventSource, timers, or router side effects", () => {
    const fetchMock = vi.fn();
    const eventSourceMock = vi.fn();
    const timerSpy = vi.spyOn(globalThis, "setTimeout");
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("EventSource", eventSourceMock);

    render(
      <ActiveRunStatus status="running" stage="planning" activity={null} />,
    );

    expect(fetchMock).not.toHaveBeenCalled();
    expect(eventSourceMock).not.toHaveBeenCalled();
    expect(timerSpy).not.toHaveBeenCalled();
  });
});
