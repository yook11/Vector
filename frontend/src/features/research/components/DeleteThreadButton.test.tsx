import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DeleteThreadButton } from "./DeleteThreadButton";
import {
  ResearchNavigationBoundary,
  useResearchNavigation,
} from "./ResearchNavigationBoundary";
import {
  ResearchOperationProvider,
  useResearchOperation,
} from "./ResearchOperationBoundary";
import {
  ResearchSubmissionProvider,
  useResearchSubmission,
} from "./ResearchSubmissionBoundary";

const mocks = vi.hoisted(() => ({
  push: vi.fn(),
  deleteThread: vi.fn(),
  toastError: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/research/current",
  useSearchParams: () => new URLSearchParams(),
  useRouter: () => ({ push: mocks.push }),
}));

vi.mock("../api/delete-research-thread", () => ({
  deleteResearchThread: mocks.deleteThread,
}));

vi.mock("@/lib/utils/toast-error", () => ({
  toastError: mocks.toastError,
}));

type Deferred = {
  promise: Promise<void>;
  reject: (reason: unknown) => void;
};

function createDeferred(): Deferred {
  let rejectPromise: ((reason: unknown) => void) | undefined;
  const promise = new Promise<void>((_resolve, reject) => {
    rejectPromise = reject;
  });
  return {
    promise,
    reject: (reason) => rejectPromise?.(reason),
  };
}

function PendingDriver({ start }: { start: boolean }) {
  const { navigate } = useResearchNavigation();
  useEffect(() => {
    if (!start) return;
    navigate({
      kind: "thread",
      href: "/research/target",
      threadId: "00000000-0000-4000-a000-000000000002",
      label: "Target",
    });
  }, [navigate, start]);
  return null;
}

function SubmissionPendingDriver({ start }: { start: boolean }) {
  const { beginSubmission } = useResearchSubmission();
  useEffect(() => {
    if (start) beginSubmission();
  }, [beginSubmission, start]);
  return null;
}

function OperationDrivers() {
  const { operation } = useResearchOperation();
  const { beginSubmission } = useResearchSubmission();
  const { navigate } = useResearchNavigation();

  return (
    <>
      <button type="button" onClick={() => beginSubmission()}>
        submission claim
      </button>
      <button
        type="button"
        onClick={() =>
          navigate({
            kind: "thread",
            href: "/research/target",
            threadId: "00000000-0000-4000-a000-000000000002",
            label: "Target",
          })
        }
      >
        navigation claim
      </button>
      <output data-testid="operation">{operation ?? "idle"}</output>
    </>
  );
}

function DeleteHarness({
  showDelete = true,
  startPending,
  startSubmission = false,
}: {
  showDelete?: boolean;
  startPending: boolean;
  startSubmission?: boolean;
}) {
  return (
    <ResearchOperationProvider>
      <ResearchSubmissionProvider>
        <ResearchNavigationBoundary sidebar={<aside>一覧</aside>}>
          <PendingDriver start={startPending} />
          <SubmissionPendingDriver start={startSubmission} />
          <OperationDrivers />
          {showDelete ? (
            <DeleteThreadButton threadId="current" title="Current thread" />
          ) : null}
        </ResearchNavigationBoundary>
      </ResearchSubmissionProvider>
    </ResearchOperationProvider>
  );
}

beforeEach(() => {
  mocks.push.mockReset();
  mocks.deleteThread.mockReset();
  mocks.toastError.mockReset();
});

describe("DeleteThreadButton navigation lock", () => {
  it("navigation pendingでdialog triggerをdisabledにする", async () => {
    const view = render(<DeleteHarness startPending={false} />);
    expect(
      screen.getByRole("button", { name: "スレッドを削除" }),
    ).toBeEnabled();

    view.rerender(<DeleteHarness startPending />);

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "スレッドを削除" }),
      ).toBeDisabled(),
    );
  });

  it("dialog表示後にnavigation pendingになってもdelete actionをdisabledにする", async () => {
    const user = userEvent.setup();
    const view = render(<DeleteHarness startPending={false} />);
    await user.click(screen.getByRole("button", { name: "スレッドを削除" }));
    expect(screen.getByRole("button", { name: "削除" })).toBeEnabled();

    view.rerender(<DeleteHarness startPending />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "削除" })).toBeDisabled(),
    );
    expect(mocks.deleteThread).not.toHaveBeenCalled();
  });

  it("submission pending中はdialog triggerをdisabledにする", async () => {
    render(<DeleteHarness startPending={false} startSubmission />);

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "スレッドを削除" }),
      ).toBeDisabled(),
    );
    expect(mocks.deleteThread).not.toHaveBeenCalled();
  });
});

describe("DeleteThreadButton deletion progress", () => {
  it.each([
    {
      first: "delete",
      order: ["delete", "submission", "navigation"] as const,
      expectedOperation: "delete",
      expectedDeleteCalls: 1,
      expectedPushCalls: 0,
    },
    {
      first: "submission",
      order: ["submission", "delete", "navigation"] as const,
      expectedOperation: "submission",
      expectedDeleteCalls: 0,
      expectedPushCalls: 0,
    },
    {
      first: "navigation",
      order: ["navigation", "delete", "submission"] as const,
      expectedOperation: "navigation",
      expectedDeleteCalls: 0,
      expectedPushCalls: 1,
    },
  ])("同一tickで$firstを先に開始したときfirst claimだけを成立させる", async ({
    order,
    expectedOperation,
    expectedDeleteCalls,
    expectedPushCalls,
  }) => {
    const deferred = createDeferred();
    mocks.deleteThread.mockReturnValue(deferred.promise);
    const user = userEvent.setup();
    render(<DeleteHarness startPending={false} />);
    const submissionControl = screen.getByRole("button", {
      name: "submission claim",
    });
    const navigationControl = screen.getByRole("button", {
      name: "navigation claim",
    });
    await user.click(screen.getByRole("button", { name: "スレッドを削除" }));

    const controls = {
      delete: screen.getByRole("button", { name: "削除" }),
      submission: submissionControl,
      navigation: navigationControl,
    };
    act(() => {
      for (const operation of order) controls[operation].click();
    });

    await waitFor(() =>
      expect(screen.getByTestId("operation")).toHaveTextContent(
        expectedOperation,
      ),
    );
    expect(mocks.deleteThread).toHaveBeenCalledTimes(expectedDeleteCalls);
    expect(mocks.push).toHaveBeenCalledTimes(expectedPushCalls);
  });

  it("unresolved delete中はdialog内のactionを削除中としてlockし二重submitしない", async () => {
    const deferred = createDeferred();
    mocks.deleteThread.mockReturnValue(deferred.promise);
    const user = userEvent.setup();
    render(<DeleteHarness startPending={false} />);

    const trigger = screen.getByRole("button", { name: "スレッドを削除" });
    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "削除" }));

    await waitFor(() => expect(mocks.deleteThread).toHaveBeenCalledTimes(1));
    const dialog = screen.getByRole("alertdialog");
    const action = within(dialog).getByRole("button", { name: "削除中…" });
    const cancel = within(dialog).getByRole("button", { name: "キャンセル" });
    const spinner = action.querySelector<SVGElement>('svg[aria-hidden="true"]');

    expect(dialog).toBeVisible();
    expect(action).toBeDisabled();
    expect(action).toHaveAttribute("aria-busy", "true");
    expect(cancel).toBeDisabled();
    expect(trigger).toBeDisabled();
    expect(spinner).toBeInTheDocument();
    expect(spinner).toHaveClass("animate-spin", "motion-reduce:animate-none");

    await user.click(action);
    expect(mocks.deleteThread).toHaveBeenCalledTimes(1);
  });

  it("delete reject後はdialogを閉じずidle actionへ戻し既存toastを出す", async () => {
    const deferred = createDeferred();
    const error = new Error("delete failed");
    mocks.deleteThread.mockReturnValue(deferred.promise);
    const user = userEvent.setup();
    render(<DeleteHarness startPending={false} />);

    const trigger = screen.getByRole("button", { name: "スレッドを削除" });
    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "削除" }));
    await waitFor(() => expect(mocks.deleteThread).toHaveBeenCalledTimes(1));

    deferred.reject(error);

    await waitFor(() => {
      const dialog = screen.getByRole("alertdialog");
      const action = within(dialog).getByRole("button", { name: "削除" });
      expect(action).toBeEnabled();
      expect(action).toHaveAttribute("aria-busy", "false");
    });
    expect(screen.getByRole("button", { name: "キャンセル" })).toBeEnabled();
    expect(trigger).toBeEnabled();
    expect(mocks.toastError).toHaveBeenCalledWith(
      error,
      "スレッドを削除できませんでした",
    );

    mocks.deleteThread.mockReturnValue(new Promise(() => undefined));
    await user.click(screen.getByRole("button", { name: "削除" }));
    await waitFor(() => expect(mocks.deleteThread).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("operation")).toHaveTextContent("delete");
  });

  it("delete button unmountでclaimをreleaseしてnavigationを開始可能にする", async () => {
    mocks.deleteThread.mockReturnValue(new Promise(() => undefined));
    const user = userEvent.setup();
    const view = render(<DeleteHarness startPending={false} />);
    await user.click(screen.getByRole("button", { name: "スレッドを削除" }));
    await user.click(screen.getByRole("button", { name: "削除" }));
    await waitFor(() =>
      expect(screen.getByTestId("operation")).toHaveTextContent("delete"),
    );

    view.rerender(<DeleteHarness showDelete={false} startPending={false} />);
    await waitFor(() =>
      expect(screen.getByTestId("operation")).toHaveTextContent("idle"),
    );
    await user.click(screen.getByRole("button", { name: "navigation claim" }));

    expect(mocks.push).toHaveBeenCalledWith("/research/target");
    expect(screen.getByTestId("operation")).toHaveTextContent("navigation");
  });

  it("delete Action resolve後はredirect commitまでdelete claimを保持する", async () => {
    mocks.deleteThread.mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(<DeleteHarness startPending={false} />);
    const navigationControl = screen.getByRole("button", {
      name: "navigation claim",
    });
    await user.click(screen.getByRole("button", { name: "スレッドを削除" }));
    await user.click(screen.getByRole("button", { name: "削除" }));
    await waitFor(() => expect(mocks.deleteThread).toHaveBeenCalledTimes(1));

    expect(screen.getByTestId("operation")).toHaveTextContent("delete");
    expect(screen.getByRole("button", { name: "削除中…" })).toBeDisabled();
    act(() => navigationControl.click());
    expect(mocks.push).not.toHaveBeenCalled();
  });
});
