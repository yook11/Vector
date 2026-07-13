import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ResearchLiveAnnouncer } from "./ResearchLiveAnnouncer";

const RUN_ONE = "00000000-0000-4000-a000-000000000010";
const THREAD_ONE = "00000000-0000-4000-a000-000000000001";
const THREAD_TWO = "00000000-0000-4000-a000-000000000002";

describe("ResearchLiveAnnouncer", () => {
  it("keeps one stable polite atomic region and announces observed completion exactly once", () => {
    const { rerender } = render(
      <ResearchLiveAnnouncer
        threadId={THREAD_ONE}
        activeRunId={RUN_ONE}
        completedRunIds={[]}
      />,
    );
    const announcer = screen.getByRole("status");
    expect(announcer).toHaveClass("sr-only");
    expect(announcer).toHaveAttribute("aria-live", "polite");
    expect(announcer).toHaveAttribute("aria-atomic", "true");

    rerender(
      <ResearchLiveAnnouncer
        threadId={THREAD_ONE}
        activeRunId={null}
        completedRunIds={[RUN_ONE]}
      />,
    );
    expect(screen.getByRole("status")).toBe(announcer);
    expect(announcer).toHaveTextContent("回答が完了しました");

    rerender(
      <ResearchLiveAnnouncer
        threadId={THREAD_ONE}
        activeRunId={null}
        completedRunIds={[RUN_ONE]}
      />,
    );
    expect(screen.getByRole("status")).toBe(announcer);
    expect(screen.getAllByText("回答が完了しました")).toHaveLength(1);
  });

  it("keeps an initially completed thread and a revisit silent", () => {
    const firstVisit = render(
      <ResearchLiveAnnouncer
        threadId={THREAD_ONE}
        activeRunId={null}
        completedRunIds={[RUN_ONE]}
      />,
    );
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    firstVisit.unmount();

    render(
      <ResearchLiveAnnouncer
        threadId={THREAD_ONE}
        activeRunId={null}
        completedRunIds={[RUN_ONE]}
      />,
    );
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("does not replace or mutate the region for ordinary rerenders", () => {
    const { rerender } = render(
      <ResearchLiveAnnouncer
        threadId={THREAD_ONE}
        activeRunId={RUN_ONE}
        completedRunIds={[]}
      />,
    );
    const announcer = screen.getByRole("status");

    rerender(
      <ResearchLiveAnnouncer
        threadId={THREAD_ONE}
        activeRunId={RUN_ONE}
        completedRunIds={[]}
      />,
    );
    rerender(
      <ResearchLiveAnnouncer
        threadId={THREAD_ONE}
        activeRunId={RUN_ONE}
        completedRunIds={[]}
      />,
    );

    expect(screen.getByRole("status")).toBe(announcer);
    expect(announcer).toBeEmptyDOMElement();
  });

  it("does not announce an old run after leaving its thread and revisiting", () => {
    const { rerender } = render(
      <ResearchLiveAnnouncer
        threadId={THREAD_ONE}
        activeRunId={RUN_ONE}
        completedRunIds={[]}
      />,
    );

    rerender(
      <ResearchLiveAnnouncer
        threadId={THREAD_TWO}
        activeRunId={null}
        completedRunIds={[]}
      />,
    );
    rerender(
      <ResearchLiveAnnouncer
        threadId={THREAD_ONE}
        activeRunId={null}
        completedRunIds={[RUN_ONE]}
      />,
    );

    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });
});
