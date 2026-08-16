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
    ["running", "safety_check", "検証中"],
    ["running", "context_resolution", "コンテキストを整理中"],
    ["running", "planning", "計画中"],
    ["running", "evidence_collection", "情報収集中"],
    ["running", "evidence_review", "情報を選別中"],
    ["running", "answering", "回答作成中"],
  ] as const)("renders %s / %s as %s", (status, stage, text) => {
    render(<ActiveRunStatus status={status} stage={stage} activity={null} />);

    expect(screen.getByText(text)).toBeInTheDocument();
  });

  it.each([
    [
      "evidence_collection",
      {
        type: "evidence_collection.internal_search_started",
        queryCount: 2,
      },
      "関連記事を検索中",
    ],
    [
      "evidence_collection",
      {
        type: "evidence_collection.internal_search_completed",
        hitCount: 8,
      },
      "関連記事8件を確認",
    ],
    [
      "evidence_collection",
      {
        type: "evidence_collection.external_search_queries_generated",
        taskIndex: 0,
        queries: ["NVIDIA AI", "半導体需要"],
      },
      "“NVIDIA AI” など2件を検索中",
    ],
    [
      "evidence_collection",
      {
        type: "evidence_collection.external_search_hits_fetched",
        taskIndex: 1,
        hitCount: 12,
      },
      "候補12件を取得",
    ],
    [
      "evidence_review",
      {
        type: "evidence_review.selected",
        evidenceCount: 4,
      },
      "根拠4件を選別",
    ],
    [
      "planning",
      {
        type: "context_resolution.question_resolved",
        standaloneQuestion: "NVIDIAの発表は株価へどう影響する？",
      },
      "“NVIDIAの発表は株価へどう影響する？”について調査中",
    ],
    [
      "context_resolution",
      {
        type: "context_resolution.question_resolved",
        standaloneQuestion: "NVIDIAの発表は株価へどう影響する？",
      },
      "“NVIDIAの発表は株価へどう影響する？”について調査中",
    ],
  ] satisfies ReadonlyArray<
    readonly [
      (
        | "planning"
        | "context_resolution"
        | "evidence_collection"
        | "evidence_review"
      ),
      ResearchLiveActivity,
      string,
    ]
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
        stage="evidence_collection"
        activity={{
          type: "evidence_collection.external_search_hits_fetched",
          taskIndex: 0,
          hitCount: 8,
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
        stage="evidence_collection"
        activity={{
          type: "evidence_collection.external_search_queries_generated",
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
        stage="answering"
        activity={{
          type: "evidence_collection.external_search_hits_fetched",
          taskIndex: 0,
          hitCount: 8,
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
