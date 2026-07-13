import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ResearchLiveAnnouncer } from "./ResearchLiveAnnouncer";

const RUN_ONE = "00000000-0000-4000-a000-000000000010";
const THREAD_ONE = "00000000-0000-4000-a000-000000000001";
const THREAD_TWO = "00000000-0000-4000-a000-000000000002";

describe("ResearchLiveAnnouncer", () => {
  it("announces an observed active run exactly once when the same thread becomes completed", () => {
    const { rerender } = render(
      <ResearchLiveAnnouncer
        threadId={THREAD_ONE}
        activeRunId={RUN_ONE}
        completedRunIds={[]}
      />,
    );
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    rerender(
      <ResearchLiveAnnouncer
        threadId={THREAD_ONE}
        activeRunId={null}
        completedRunIds={[RUN_ONE]}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("回答が完了しました");
    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");

    rerender(
      <ResearchLiveAnnouncer
        threadId={THREAD_ONE}
        activeRunId={null}
        completedRunIds={[RUN_ONE]}
      />,
    );
    expect(screen.getAllByText("回答が完了しました")).toHaveLength(1);
  });

  it("does not announce an initially completed thread or a revisit", () => {
    const firstVisit = render(
      <ResearchLiveAnnouncer
        threadId={THREAD_ONE}
        activeRunId={null}
        completedRunIds={[RUN_ONE]}
      />,
    );
    expect(screen.queryByText("回答が完了しました")).not.toBeInTheDocument();
    firstVisit.unmount();

    render(
      <ResearchLiveAnnouncer
        threadId={THREAD_ONE}
        activeRunId={null}
        completedRunIds={[RUN_ONE]}
      />,
    );
    expect(screen.queryByText("回答が完了しました")).not.toBeInTheDocument();
  });

  it("does not announce ordinary rerenders such as activity or delta updates", () => {
    const { rerender } = render(
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
    rerender(
      <ResearchLiveAnnouncer
        threadId={THREAD_ONE}
        activeRunId={RUN_ONE}
        completedRunIds={[]}
      />,
    );

    expect(screen.queryByText("回答が完了しました")).not.toBeInTheDocument();
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

    expect(screen.queryByText("回答が完了しました")).not.toBeInTheDocument();
  });
});
