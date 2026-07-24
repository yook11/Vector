import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type ComponentProps, createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PaginatedResearchThreadResponse } from "@/types/types.gen";
import { ResearchNavigationBoundary } from "./ResearchNavigationBoundary";
import { ResearchOperationProvider } from "./ResearchOperationBoundary";
import { ResearchSidebar } from "./ResearchSidebar";
import { ResearchSubmissionProvider } from "./ResearchSubmissionBoundary";

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

vi.mock("next/link", () => ({
  default: ({
    onNavigate,
    ...props
  }: ComponentProps<"a"> & {
    onNavigate?: (event: { preventDefault: () => void }) => void;
  }) =>
    createElement("a", {
      ...props,
      onClick: (event) => {
        if (
          event.button === 0 &&
          !event.metaKey &&
          !event.ctrlKey &&
          !event.shiftKey &&
          !event.altKey
        ) {
          onNavigate?.({ preventDefault: () => event.preventDefault() });
        }
      },
    }),
}));

const A_ID = "00000000-0000-4000-a000-000000000001";
const B_ID = "00000000-0000-4000-a000-000000000002";
const THREADS = {
  items: [
    {
      threadId: A_ID,
      title: "Thread A",
      updatedAt: "2026-07-13T01:00:00Z",
      hasActiveRun: false,
    },
    {
      threadId: B_ID,
      title: "Thread B",
      updatedAt: "2026-07-12T01:00:00Z",
      hasActiveRun: false,
    },
  ],
  total: 3,
  page: 1,
  perPage: 2,
  totalPages: 2,
} satisfies PaginatedResearchThreadResponse;

interface MatchMediaController {
  setWidth: (width: number) => void;
}

function mediaWidth(value: string, unit: string): number {
  return Number(value) * (unit === "rem" ? 16 : 1);
}

function mediaMatches(query: string, width: number): boolean {
  const min = query.match(/min-width:\s*([\d.]+)(px|rem)/);
  const max = query.match(/max-width:\s*([\d.]+)(px|rem)/);
  if (min?.[1] && min[2] && width < mediaWidth(min[1], min[2])) return false;
  if (max?.[1] && max[2] && width > mediaWidth(max[1], max[2])) return false;
  return true;
}

function installMatchMedia(initialWidth: number): MatchMediaController {
  let width = initialWidth;
  const queries = new Map<
    string,
    {
      mediaQuery: MediaQueryList;
      listeners: Set<(event: MediaQueryListEvent) => void>;
      previousMatches: boolean;
    }
  >();

  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    writable: true,
    value: initialWidth,
  });

  vi.stubGlobal("matchMedia", (query: string) => {
    const existing = queries.get(query);
    if (existing) return existing.mediaQuery;

    const listeners = new Set<(event: MediaQueryListEvent) => void>();
    const mediaQuery = {
      get matches() {
        return mediaMatches(query, width);
      },
      media: query,
      onchange: null,
      addEventListener: (
        type: string,
        listener: EventListenerOrEventListenerObject,
      ) => {
        if (type === "change" && typeof listener === "function") {
          listeners.add(listener as (event: MediaQueryListEvent) => void);
        }
      },
      removeEventListener: (
        type: string,
        listener: EventListenerOrEventListenerObject,
      ) => {
        if (type === "change" && typeof listener === "function") {
          listeners.delete(listener as (event: MediaQueryListEvent) => void);
        }
      },
      addListener: (listener: (event: MediaQueryListEvent) => void) => {
        listeners.add(listener);
      },
      removeListener: (listener: (event: MediaQueryListEvent) => void) => {
        listeners.delete(listener);
      },
      dispatchEvent: () => true,
    } as unknown as MediaQueryList;
    queries.set(query, {
      mediaQuery,
      listeners,
      previousMatches: mediaQuery.matches,
    });
    return mediaQuery;
  });

  return {
    setWidth(nextWidth: number) {
      act(() => {
        width = nextWidth;
        window.innerWidth = nextWidth;
        for (const state of queries.values()) {
          const matches = state.mediaQuery.matches;
          if (matches === state.previousMatches) continue;
          state.previousMatches = matches;
          const event = new Event("change") as MediaQueryListEvent;
          Object.defineProperties(event, {
            matches: { value: matches },
            media: { value: state.mediaQuery.media },
          });
          for (const listener of state.listeners) listener(event);
        }
        window.dispatchEvent(new Event("resize"));
      });
    },
  };
}

function renderHistory() {
  return render(
    <ResearchOperationProvider>
      <ResearchSubmissionProvider>
        <ResearchNavigationBoundary
          sidebar={
            <ResearchSidebar
              threads={THREADS}
              activeThreadId={A_ID}
              limit={2}
            />
          }
        >
          <section>
            <h2>Thread A 本文</h2>
          </section>
        </ResearchNavigationBoundary>
      </ResearchSubmissionProvider>
    </ResearchOperationProvider>,
  );
}

function historyToggle(): HTMLButtonElement {
  return screen.getByRole("button", {
    name: /履歴を(?:開く|閉じる)/,
  });
}

async function openCompactDrawer() {
  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: "履歴を開く" }));
  return screen.getByRole("dialog", { name: "リサーチ履歴" });
}

beforeEach(() => {
  mocks.pathname = `/research/${A_ID}`;
  mocks.search = "limit=2";
  mocks.push.mockReset();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Research workspace history", () => {
  it("desktopでは履歴を初期表示し、native toggleでDOMとfocus順から除去して再表示する", async () => {
    installMatchMedia(1024);
    const user = userEvent.setup();
    renderHistory();

    const toggle = screen.getByRole("button", { name: "履歴を閉じる" });
    expect(toggle).toHaveAttribute("aria-controls", "research-history");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(
      document.querySelector("aside#research-history"),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Thread B/ })).toBeInTheDocument();

    await user.click(toggle);

    expect(historyToggle()).toHaveAccessibleName("履歴を開く");
    expect(historyToggle()).toHaveAttribute("aria-expanded", "false");
    expect(
      document.querySelector("aside#research-history"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /Thread B/ }),
    ).not.toBeInTheDocument();

    await user.click(historyToggle());

    expect(historyToggle()).toHaveAccessibleName("履歴を閉じる");
    expect(historyToggle()).toHaveAttribute("aria-expanded", "true");
    expect(
      document.querySelector("aside#research-history"),
    ).toBeInTheDocument();
  });

  it("workspaceをremountするとdesktop履歴は初期openへ戻る", async () => {
    installMatchMedia(1440);
    const user = userEvent.setup();
    const first = renderHistory();
    await user.click(screen.getByRole("button", { name: "履歴を閉じる" }));
    expect(
      document.querySelector("aside#research-history"),
    ).not.toBeInTheDocument();

    first.unmount();
    renderHistory();

    expect(
      screen.getByRole("button", { name: "履歴を閉じる" }),
    ).toHaveAttribute("aria-expanded", "true");
    expect(
      document.querySelector("aside#research-history"),
    ).toBeInTheDocument();
  });

  it("compactではinline asideを描画せず、左SheetをEscapeとclose buttonで閉じてtoggleへfocusを返す", async () => {
    installMatchMedia(1023);
    const user = userEvent.setup();
    renderHistory();

    const toggle = screen.getByRole("button", { name: "履歴を開く" });
    expect(toggle).toHaveAttribute("aria-controls", "research-history");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(
      document.querySelector("aside#research-history"),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.click(toggle);

    const dialog = screen.getByRole("dialog", { name: "リサーチ履歴" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAttribute("data-slot", "sheet-content");
    expect(dialog).toHaveClass("left-0", "border-r");
    expect(dialog).not.toHaveClass("right-0", "border-l");
    expect(
      within(dialog).getByRole("heading", { name: "リサーチ履歴" }),
    ).toBeVisible();
    expect(toggle).toHaveAttribute("aria-expanded", "true");

    await user.keyboard("{Escape}");

    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(toggle).toHaveFocus();
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    await user.click(toggle);
    const reopenedDialog = screen.getByRole("dialog", {
      name: "リサーチ履歴",
    });
    await user.click(
      within(reopenedDialog).getByRole("button", { name: /閉じる|Close/ }),
    );

    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(toggle).toHaveFocus();
  });

  it("viewport modeを切り替えるとdrawerだけを閉じ、同じmountのdesktop open stateを復元する", async () => {
    const viewport = installMatchMedia(1200);
    const user = userEvent.setup();
    renderHistory();
    expect(
      document.querySelector("aside#research-history"),
    ).toBeInTheDocument();

    viewport.setWidth(800);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "履歴を開く" }));
    expect(
      screen.getByRole("dialog", { name: "リサーチ履歴" }),
    ).toBeInTheDocument();

    viewport.setWidth(1200);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(
      document.querySelector("aside#research-history"),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "履歴を閉じる" }));
    viewport.setWidth(800);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    viewport.setWidth(1200);
    expect(
      document.querySelector("aside#research-history"),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "履歴を開く" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });

  it("compact drawerのactive threadはnavigationせずdrawerだけを閉じる", async () => {
    installMatchMedia(390);
    renderHistory();
    const dialog = await openCompactDrawer();

    await userEvent
      .setup()
      .click(within(dialog).getByRole("link", { name: /Thread A/ }));

    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(mocks.push).not.toHaveBeenCalled();
  });

  it.each([
    ["別thread", /Thread B/, `/research/${B_ID}?limit=2`],
    ["新規thread", "新しいスレッド", "/research"],
  ])("compact drawerの%sはnavigation受付時にdrawerを閉じる", async (_label, name, href) => {
    installMatchMedia(767);
    renderHistory();
    const dialog = await openCompactDrawer();

    await userEvent.setup().click(within(dialog).getByRole("link", { name }));

    expect(mocks.push).toHaveBeenCalledTimes(1);
    expect(mocks.push).toHaveBeenCalledWith(href);
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
  });

  it("compact drawerのさらに表示はnavigation受付後もdrawerを開いたままにする", async () => {
    installMatchMedia(768);
    renderHistory();
    const dialog = await openCompactDrawer();

    fireEvent.click(within(dialog).getByRole("link", { name: "さらに表示" }));

    expect(mocks.push).toHaveBeenCalledTimes(1);
    expect(mocks.push).toHaveBeenCalledWith(`/research/${A_ID}?limit=3`);
    expect(
      screen.getByRole("dialog", { name: "リサーチ履歴" }),
    ).toBeInTheDocument();
  });

  it("sidebarはheaderとfooterを固定し、label付きnavだけをscroll ownerにする", () => {
    installMatchMedia(1024);
    renderHistory();

    const aside = document.querySelector("aside#research-history");
    expect(aside).toBeInTheDocument();
    const navigation = screen.getByRole("navigation", {
      name: "リサーチ履歴",
    });
    expect(navigation).toHaveClass("overflow-y-auto", "overscroll-contain");

    const header = screen.getByRole("heading", { name: "Agent threads" })
      .parentElement?.parentElement;
    const footer = screen.getByRole("link", {
      name: "さらに表示",
    }).parentElement;
    expect(header).not.toHaveClass("overflow-y-auto");
    expect(footer).not.toHaveClass("overflow-y-auto");
    expect(aside?.querySelectorAll("nav")).toHaveLength(1);
  });
});
