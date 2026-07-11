import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ResearchNavigationBoundary,
  type ResearchNavigationTarget,
  useResearchNavigation,
} from "./ResearchNavigationBoundary";

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
  return (
    <>
      <button type="button" onClick={() => navigate(TARGET_B)}>
        Bへ移動
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
    </>
  );
}

function renderBoundary() {
  return render(
    <ResearchNavigationBoundary sidebar={<aside>スレッド一覧</aside>}>
      <section>
        <p>旧Thread A本文</p>
        <NavigationProbe />
      </section>
    </ResearchNavigationBoundary>,
  );
}

beforeEach(() => {
  mocks.pathname = "/research/00000000-0000-4000-a000-000000000001";
  mocks.search = "limit=2";
  mocks.push.mockReset();
});

describe("ResearchNavigationBoundary", () => {
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

  it("target URLのcommit後にbusy stateを解除する", async () => {
    const user = userEvent.setup();
    const view = renderBoundary();
    await user.click(screen.getByRole("button", { name: "Bへ移動" }));

    mocks.pathname = "/research/00000000-0000-4000-a000-000000000002";
    view.rerender(
      <ResearchNavigationBoundary sidebar={<aside>スレッド一覧</aside>}>
        <section>
          <p>Thread B本文</p>
          <NavigationProbe />
        </section>
      </ResearchNavigationBoundary>,
    );

    await waitFor(() =>
      expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "false"),
    );
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });
});
