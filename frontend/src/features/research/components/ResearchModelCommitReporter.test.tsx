import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ResearchMessageRun,
  ResearchThreadDetail,
} from "@/types/types.gen";
import { ResearchModelCommitReporter } from "./ResearchModelCommitReporter";
import { ResearchOperationProvider } from "./ResearchOperationBoundary";
import {
  ResearchSubmissionProvider,
  useResearchSubmission,
} from "./ResearchSubmissionBoundary";

const mocks = vi.hoisted(() => ({
  replace: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: mocks.replace }),
}));

const THREAD_ID = "00000000-0000-4000-a000-000000000001";
const OTHER_THREAD_ID = "00000000-0000-4000-a000-000000000002";
const RUN_ID = "00000000-0000-4000-a000-000000000011";
const OTHER_RUN_ID = "00000000-0000-4000-a000-000000000012";

function threadWithRun(
  status: ResearchMessageRun["status"],
  threadId = THREAD_ID,
  runId = RUN_ID,
): ResearchThreadDetail {
  return {
    threadId,
    title: "Committed model",
    messages: [
      {
        role: "user",
        seq: 1,
        content: "モデルへcommitされた質問",
        createdAt: "2026-07-24T00:00:00Z",
        run: {
          runId,
          status,
          errorCode: status === "failed" ? "enqueue_failed" : null,
          progressStage:
            status === "queued" || status === "running" ? "synthesizing" : null,
        },
      },
    ],
  };
}

function SubmissionProbe() {
  const { acceptSubmission, beginSubmission, isSubmissionPending } =
    useResearchSubmission();
  return (
    <>
      <button
        type="button"
        onClick={() => {
          if (beginSubmission()) {
            acceptSubmission({ threadId: THREAD_ID, runId: RUN_ID });
          }
        }}
      >
        acceptedを記録
      </button>
      <output aria-label="submission pending">
        {isSubmissionPending ? "pending" : "settled"}
      </output>
    </>
  );
}

function SubmissionHarness({
  thread,
}: {
  thread: ResearchThreadDetail | null;
}) {
  return (
    <ResearchOperationProvider>
      <ResearchSubmissionProvider>
        <SubmissionProbe />
        <ResearchModelCommitReporter thread={thread} />
      </ResearchSubmissionProvider>
    </ResearchOperationProvider>
  );
}

beforeEach(() => {
  mocks.replace.mockReset();
});

describe("Research submission model identity", () => {
  it.each([
    "queued",
    "running",
    "failed",
    "completed",
    "policy_blocked",
  ] satisfies ResearchMessageRun["status"][])("exact targetのfirst committed modelが%sならsubmission lockをsettleする", async (status) => {
    const user = userEvent.setup();
    const view = render(<SubmissionHarness thread={null} />);

    await user.click(screen.getByRole("button", { name: "acceptedを記録" }));
    expect(
      screen.getByRole("status", { name: "submission pending" }),
    ).toHaveTextContent("pending");

    view.rerender(<SubmissionHarness thread={threadWithRun(status)} />);

    await waitFor(() =>
      expect(
        screen.getByRole("status", { name: "submission pending" }),
      ).toHaveTextContent("settled"),
    );
  });

  it.each([
    ["same threadの別run", threadWithRun("completed", THREAD_ID, OTHER_RUN_ID)],
    ["別threadのsame run", threadWithRun("completed", OTHER_THREAD_ID, RUN_ID)],
  ])("%sではsubmission lockをsettleしない", async (_label, thread) => {
    const user = userEvent.setup();
    const view = render(<SubmissionHarness thread={null} />);

    await user.click(screen.getByRole("button", { name: "acceptedを記録" }));
    view.rerender(<SubmissionHarness thread={thread} />);

    await waitFor(() =>
      expect(
        screen.getByRole("status", { name: "submission pending" }),
      ).toHaveTextContent("pending"),
    );
  });
});
