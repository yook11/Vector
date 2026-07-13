import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type ComponentProps, createElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ResearchNavigationBoundary } from "./ResearchNavigationBoundary";
import { ResearchNavigationLink } from "./ResearchNavigationLink";

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

function ThreadLinks() {
  return (
    <>
      <ResearchNavigationLink
        variant="thread"
        target={{
          kind: "thread",
          href: `/research/${A_ID}?limit=2`,
          threadId: A_ID,
          label: "Thread A",
        }}
        active
        title="Thread A"
        idleMetaLabel="7月11日 10:00"
        hasActiveRun={false}
      />
      <ResearchNavigationLink
        variant="thread"
        target={{
          kind: "thread",
          href: `/research/${B_ID}?limit=2`,
          threadId: B_ID,
          label: "Thread B",
        }}
        active={false}
        title="Thread B"
        idleMetaLabel="7月11日 09:00"
        hasActiveRun
      />
      <ResearchNavigationLink
        variant="new"
        target={{ kind: "new", href: "/research", label: "新しいスレッド" }}
      />
      <ResearchNavigationLink
        variant="more"
        target={{
          kind: "more",
          href: `/research/${A_ID}?limit=3`,
          label: "さらに表示",
        }}
      />
    </>
  );
}

function renderLinks() {
  return render(
    <ResearchNavigationBoundary sidebar={<ThreadLinks />}>
      <p>旧本文</p>
    </ResearchNavigationBoundary>,
  );
}

beforeEach(() => {
  mocks.pathname = `/research/${A_ID}`;
  mocks.search = "limit=2";
  mocks.push.mockReset();
});

describe("ResearchNavigationLink", () => {
  it("anchor semanticsとactive stateを維持する", () => {
    renderLinks();

    const active = screen.getByRole("link", { name: /Thread A/ });
    expect(active).toHaveAttribute("href", `/research/${A_ID}?limit=2`);
    expect(active).toHaveAttribute("aria-current", "page");
  });

  it("旧view queryをthread/new/more hrefへ継承せず既存limitだけを維持する", () => {
    mocks.search = "limit=2&view=sources";
    renderLinks();

    const expectations = [
      [screen.getByRole("link", { name: /Thread A/ }), "2"],
      [screen.getByRole("link", { name: /Thread B/ }), "2"],
      [screen.getByRole("link", { name: /さらに表示/ }), "3"],
      [screen.getByRole("link", { name: "新しいスレッド" }), null],
    ] as const;
    for (const [link, limit] of expectations) {
      const href = link.getAttribute("href");
      expect(href).not.toBeNull();
      const url = new URL(href ?? "", "http://research.local");
      expect(url.searchParams.get("limit")).toBe(limit);
      expect(url.searchParams.has("view")).toBe(false);
    }
  });

  it("clicked threadをbusyにし、他のResearch navigationもlockする", async () => {
    const user = userEvent.setup();
    renderLinks();
    const target = screen.getByRole("link", { name: /Thread B/ });

    await user.click(target);

    expect(target).toHaveAttribute("aria-busy", "true");
    expect(target).toHaveAttribute("aria-disabled", "true");
    expect(target).toHaveTextContent("Thread B");
    expect(target).toHaveTextContent("読み込み中…");
    expect(target).not.toHaveTextContent("7月11日 09:00");
    expect(target).toHaveClass("ring-1", "opacity-100");
    const oldActive = screen.getByRole("link", { name: /Thread A/ });
    expect(oldActive).toHaveAttribute("aria-disabled", "true");
    expect(oldActive).toHaveClass("opacity-45");
    expect(screen.getByRole("link", { name: /さらに表示/ })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });

  it("active linkとpending中の後続activationを拒否する", async () => {
    const user = userEvent.setup();
    renderLinks();

    await user.click(screen.getByRole("link", { name: /Thread A/ }));
    expect(mocks.push).not.toHaveBeenCalled();

    await user.click(screen.getByRole("link", { name: /Thread B/ }));
    await user.click(screen.getByRole("link", { name: /さらに表示/ }));
    expect(mocks.push).toHaveBeenCalledTimes(1);
  });

  it("modifierとmiddle clickをpreventしない", () => {
    renderLinks();
    const target = screen.getByRole("link", { name: /Thread B/ });

    expect(fireEvent.click(target, { ctrlKey: true })).toBe(true);
    expect(
      fireEvent(
        target,
        new MouseEvent("auxclick", {
          bubbles: true,
          button: 1,
          cancelable: true,
        }),
      ),
    ).toBe(true);
    expect(mocks.push).not.toHaveBeenCalled();
  });

  it("/researchではqueryの有無にかかわらずnew navigationを開始しない", async () => {
    mocks.pathname = "/research";
    const user = userEvent.setup();
    renderLinks();

    await user.click(screen.getByRole("link", { name: "新しいスレッド" }));

    expect(mocks.push).not.toHaveBeenCalled();
  });
});
