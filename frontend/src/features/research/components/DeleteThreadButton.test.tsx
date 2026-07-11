import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DeleteThreadButton } from "./DeleteThreadButton";
import {
  ResearchNavigationBoundary,
  useResearchNavigation,
} from "./ResearchNavigationBoundary";

const mocks = vi.hoisted(() => ({
  push: vi.fn(),
  deleteThread: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/research/current",
  useSearchParams: () => new URLSearchParams(),
  useRouter: () => ({ push: mocks.push }),
}));

vi.mock("../api/delete-research-thread", () => ({
  deleteResearchThread: mocks.deleteThread,
}));

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

function DeleteHarness({ startPending }: { startPending: boolean }) {
  return (
    <ResearchNavigationBoundary sidebar={<aside>一覧</aside>}>
      <PendingDriver start={startPending} />
      <DeleteThreadButton threadId="current" title="Current thread" />
    </ResearchNavigationBoundary>
  );
}

beforeEach(() => {
  mocks.push.mockReset();
  mocks.deleteThread.mockReset();
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
});
