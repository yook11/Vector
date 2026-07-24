import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ResearchNavigationBoundary,
  type ResearchNavigationTarget,
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
  pathname: "/research/00000000-0000-4000-a000-000000000001",
  search: "limit=2",
  push: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => mocks.pathname,
  useSearchParams: () => new URLSearchParams(mocks.search),
  useRouter: () => ({ push: mocks.push }),
}));

const TARGET_A: ResearchNavigationTarget = {
  kind: "thread",
  href: "/research/00000000-0000-4000-a000-000000000001?limit=2",
  threadId: "00000000-0000-4000-a000-000000000001",
  label: "Thread A",
};

const TARGET_B: ResearchNavigationTarget = {
  kind: "thread",
  href: "/research/00000000-0000-4000-a000-000000000002?limit=2",
  threadId: "00000000-0000-4000-a000-000000000002",
  label: "Thread B",
};

function NavigationProbe() {
  const { navigate } = useResearchNavigation();
  const { beginSubmission } = useResearchSubmission();
  const { claimOperation } = useResearchOperation();
  const [navigationResult, setNavigationResult] = useState<boolean | null>(
    null,
  );
  return (
    <>
      <button type="button" onClick={() => beginSubmission()}>
        submission開始
      </button>
      <button type="button" onClick={() => claimOperation("delete")}>
        delete開始
      </button>
      <button type="button" onClick={() => navigate(TARGET_B)}>
        Bへ移動
      </button>
      <button
        type="button"
        onClick={() => setNavigationResult(navigate(TARGET_B))}
      >
        Bへ移動結果
      </button>
      <button
        type="button"
        onClick={() => {
          navigate(TARGET_B);
          navigate(TARGET_A);
        }}
      >
        二重移動
      </button>
      <span data-testid="navigation-result">
        {navigationResult === null ? "未実行" : String(navigationResult)}
      </span>
    </>
  );
}

function renderBoundary() {
  return render(
    <ResearchOperationProvider>
      <ResearchSubmissionProvider>
        <ResearchNavigationBoundary sidebar={<aside>スレッド一覧</aside>}>
          <section>
            <p>旧Thread A本文</p>
            <NavigationProbe />
          </section>
        </ResearchNavigationBoundary>
      </ResearchSubmissionProvider>
    </ResearchOperationProvider>,
  );
}

beforeEach(() => {
  mocks.pathname = "/research/00000000-0000-4000-a000-000000000001";
  mocks.search = "limit=2";
  mocks.push.mockReset();
});

describe("ResearchNavigationBoundary", () => {
  it("親から与えられた高さと幅をworkspaceの内部scroll境界へ渡す", () => {
    renderBoundary();

    const workspace = screen.getByRole("main");
    expect(workspace).toHaveClass("h-full", "min-h-0", "w-full");
    expect(workspace.className).not.toContain("calc(100dvh");
    expect(workspace).not.toHaveClass("mx-auto", "max-w-[1280px]");
  });

  it("idleではworkspaceと常設statusをbusyにしない", () => {
    renderBoundary();

    expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "false");
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    expect(screen.getByText("旧Thread A本文")).toBeInTheDocument();
  });

  it("遷移対象を表示しながら旧本文を保持し、正しいhrefを1回pushする", async () => {
    const user = userEvent.setup();
    renderBoundary();

    await user.click(screen.getByRole("button", { name: "Bへ移動" }));

    expect(mocks.push).toHaveBeenCalledTimes(1);
    expect(mocks.push).toHaveBeenCalledWith(TARGET_B.href);
    expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("status")).toHaveTextContent(
      "「Thread B」を読み込み中…",
    );
    expect(screen.getByText("旧Thread A本文")).toBeInTheDocument();
    expect(
      screen.getByText("「Thread B」を読み込み中…", { selector: "p" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Researchを読み込み中…")).toBeNull();
  });

  it("同一tickの後続navigationを拒否する", async () => {
    const user = userEvent.setup();
    renderBoundary();

    await user.click(screen.getByRole("button", { name: "二重移動" }));

    expect(mocks.push).toHaveBeenCalledTimes(1);
    expect(mocks.push).toHaveBeenCalledWith(TARGET_B.href);
    expect(screen.getByRole("status")).toHaveTextContent(
      "「Thread B」を読み込み中…",
    );
  });

  it("submission pending中はnavigateをfalseで拒否してrouterを呼ばない", async () => {
    const user = userEvent.setup();
    renderBoundary();

    await user.click(screen.getByRole("button", { name: "submission開始" }));
    await waitFor(() =>
      expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "true"),
    );
    await user.click(screen.getByRole("button", { name: "Bへ移動結果" }));

    expect(screen.getByTestId("navigation-result")).toHaveTextContent("false");
    expect(mocks.push).not.toHaveBeenCalled();
    expect(screen.queryByTestId("research-navigation-overlay")).toBeNull();
  });

  it("delete claim中はnavigateをfalseで拒否してrouterを呼ばない", async () => {
    const user = userEvent.setup();
    renderBoundary();

    await user.click(screen.getByRole("button", { name: "delete開始" }));
    await user.click(screen.getByRole("button", { name: "Bへ移動結果" }));

    expect(screen.getByTestId("navigation-result")).toHaveTextContent("false");
    expect(mocks.push).not.toHaveBeenCalled();
    expect(screen.queryByTestId("research-navigation-overlay")).toBeNull();
  });

  it("target URLのcommit後にbusy stateを解除する", async () => {
    const user = userEvent.setup();
    const view = renderBoundary();
    await user.click(screen.getByRole("button", { name: "Bへ移動" }));

    mocks.pathname = "/research/00000000-0000-4000-a000-000000000002";
    view.rerender(
      <ResearchOperationProvider>
        <ResearchSubmissionProvider>
          <ResearchNavigationBoundary sidebar={<aside>スレッド一覧</aside>}>
            <section>
              <p>Thread B本文</p>
              <NavigationProbe />
            </section>
          </ResearchNavigationBoundary>
        </ResearchSubmissionProvider>
      </ResearchOperationProvider>,
    );

    await waitFor(() =>
      expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "false"),
    );
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });
});
