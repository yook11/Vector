import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type {
  AnchorHTMLAttributes,
  MouseEvent as ReactMouseEvent,
  ReactNode,
} from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  pathname: "/",
  pendingByHref: new Map<string, boolean>(),
  linkNavigations: [] as string[],
  requireSession: vi.fn(),
}));

type LinkMockProps = AnchorHTMLAttributes<HTMLAnchorElement> & {
  children: ReactNode;
  href: string;
  onNavigate?: (event: { preventDefault: () => void }) => void;
  prefetch?: boolean | null;
  replace?: boolean;
  scroll?: boolean;
};

vi.mock("next/link", async () => {
  const React = await import("react");
  const PendingContext = React.createContext(false);

  function Link({
    children,
    href,
    onClick,
    onNavigate,
    prefetch: _prefetch,
    replace: _replace,
    scroll: _scroll,
    ...rest
  }: LinkMockProps) {
    const [clickedPending, setClickedPending] = React.useState(false);

    function handleClick(event: ReactMouseEvent<HTMLAnchorElement>) {
      onClick?.(event);
      if (!event.defaultPrevented && onNavigate !== undefined) {
        onNavigate({ preventDefault: () => event.preventDefault() });
      }
      if (!event.defaultPrevented) {
        mocks.linkNavigations.push(href);
        setClickedPending(true);
      }
      // JSDOM は実document navigationを実装しない。Linkの実navigationはこの
      // component testの境界外なので、Sheet stateの観測後にだけ抑止する。
      event.preventDefault();
    }

    return (
      <PendingContext.Provider
        value={mocks.pendingByHref.get(href) ?? clickedPending}
      >
        <a href={href} onClick={handleClick} {...rest}>
          {children}
        </a>
      </PendingContext.Provider>
    );
  }

  return {
    default: Link,
    useLinkStatus: () => ({ pending: React.useContext(PendingContext) }),
  };
});

vi.mock("next/navigation", () => ({
  usePathname: () => mocks.pathname,
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/lib/auth/guards", () => ({
  requireSession: () => mocks.requireSession(),
}));

import ProtectedLayout from "@/app/(protected)/layout";
import { MobileNav } from "./MobileNav";

const NAV_ITEMS = [
  { href: "/research", label: "Research", icon: "research" },
  { href: "/briefing", label: "Briefing", icon: "briefing" },
  { href: "/trends", label: "トレンド", icon: "trend" },
] as const;

async function mobileNavTree() {
  return ProtectedLayout({ children: <MobileNav items={[...NAV_ITEMS]} /> });
}

describe("MobileNav navigation settle", () => {
  beforeEach(() => {
    mocks.pathname = "/";
    mocks.pendingByHref.clear();
    mocks.linkNavigations.length = 0;
    mocks.requireSession.mockReset();
    mocks.requireSession.mockResolvedValue({ user: { id: "user-1" } });
  });

  it("pending中はSheetを保ち、settle closeだけtriggerへfocusを戻さない", async () => {
    const user = userEvent.setup();
    const view = render(await mobileNavTree());
    const trigger = screen.getByRole("button", { name: "メニュー" });

    await user.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "Vector" });
    expect(
      within(dialog).getByRole("link", { name: "Briefing" }),
    ).toHaveAttribute("href", "/briefing");

    mocks.pendingByHref.set("/briefing", true);
    view.rerender(await mobileNavTree());
    await user.click(within(dialog).getByRole("link", { name: "Briefing" }));

    await waitFor(() =>
      expect(
        screen.getByRole("dialog", { name: "Vector" }),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("status", { name: "Briefingを読み込み中…" }),
    ).toBeVisible();
    expect(
      within(screen.getByRole("dialog", { name: "Vector" })).getByRole("link", {
        name: "トレンド",
      }),
    ).toBeEnabled();

    mocks.pendingByHref.set("/briefing", false);
    view.rerender(await mobileNavTree());
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Vector" })).toBeNull(),
    );
    expect(trigger).not.toHaveFocus();

    await user.click(trigger);
    await user.keyboard("{Escape}");
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Vector" })).toBeNull(),
    );
    expect(trigger).toHaveFocus();
  });

  it("Research sectionのcurrent itemはSheetもglobal pendingも開始しない", async () => {
    mocks.pathname = "/research/thread-1";
    const user = userEvent.setup();
    render(await mobileNavTree());

    await user.click(screen.getByRole("button", { name: "メニュー" }));
    const dialog = screen.getByRole("dialog", { name: "Vector" });
    await user.click(within(dialog).getByRole("link", { name: "Research" }));

    expect(mocks.linkNavigations).toEqual([]);
    expect(screen.getByRole("dialog", { name: "Vector" })).toBeVisible();
    expect(
      screen.queryByRole("status", { name: "Researchを読み込み中…" }),
    ).toBeNull();
  });

  it("pending中のmanual close後にclosedのままsettleしても次のreopenを閉じない", async () => {
    const user = userEvent.setup();
    const view = render(await mobileNavTree());
    const trigger = screen.getByRole("button", { name: "メニュー" });

    await user.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "Vector" });
    mocks.pendingByHref.set("/briefing", true);
    view.rerender(await mobileNavTree());
    await user.click(within(dialog).getByRole("link", { name: "Briefing" }));
    await waitFor(() =>
      expect(
        screen.getByRole("status", { name: "Briefingを読み込み中…" }),
      ).toBeVisible(),
    );

    await user.keyboard("{Escape}");
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Vector" })).toBeNull(),
    );
    expect(trigger).toHaveFocus();

    mocks.pendingByHref.set("/briefing", false);
    mocks.pathname = "/briefing";
    view.rerender(await mobileNavTree());
    expect(screen.queryByRole("dialog", { name: "Vector" })).toBeNull();

    await user.click(trigger);

    await waitFor(() =>
      expect(screen.getByRole("dialog", { name: "Vector" })).toBeVisible(),
    );
    expect(
      screen.queryByRole("status", { name: "Briefingを読み込み中…" }),
    ).toBeNull();
  });
});
