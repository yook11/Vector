import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import ResearchThreadPage from "./[threadId]/page";
import ResearchPage from "./page";

vi.mock("@/components/layout/ShellMasthead", () => ({
  ShellMasthead: () => <header data-testid="shell-masthead" />,
}));

vi.mock("@/components/paper", () => ({
  PaperSurface: ({
    children,
    className,
  }: {
    children: ReactNode;
    className?: string;
  }) => (
    <div data-testid="paper-surface" className={className}>
      {children}
    </div>
  ),
  PaperTexture: () => <div data-testid="paper-texture" />,
}));

vi.mock("@/features/research", () => ({
  getResearchThreads: vi.fn().mockResolvedValue({
    items: [],
    nextCursor: null,
  }),
  loadResearchThreadPage: vi.fn().mockResolvedValue({
    state: "ready",
    threads: { items: [], nextCursor: null },
    thread: { threadId: "thread-1", title: "調査", messages: [] },
  }),
  parseResearchLimit: vi.fn().mockReturnValue(20),
  parseResearchView: vi.fn().mockReturnValue({
    view: "answer",
    canonical: true,
  }),
  ResearchUuidSchema: {
    safeParse: (value: string) => ({ success: true, data: value }),
  },
  ResearchWorkspace: () => <section data-testid="research-workspace" />,
}));

vi.mock("@/lib/auth/guards", () => ({
  requireSession: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("next/navigation", () => ({
  notFound: vi.fn(),
}));

type ResearchRouteElement = Awaited<ReturnType<typeof ResearchPage>>;

const routes: Array<{
  name: string;
  renderRoute: () => Promise<ResearchRouteElement>;
}> = [
  {
    name: "/research",
    renderRoute: () => ResearchPage({ searchParams: Promise.resolve({}) }),
  },
  {
    name: "/research/[threadId]",
    renderRoute: () =>
      ResearchThreadPage({
        params: Promise.resolve({ threadId: "thread-1" }),
        searchParams: Promise.resolve({}),
      }),
  },
];

describe.each(routes)("$name viewport shell", ({ renderRoute }) => {
  it("masthead後の残り高さだけをworkspaceへ渡す", async () => {
    render(await renderRoute());

    const shell = screen.getByTestId("paper-surface");
    const masthead = screen.getByTestId("shell-masthead");
    const workspace = screen.getByTestId("research-workspace");
    const routeViewport = workspace.parentElement;

    expect(shell).toHaveClass(
      "flex",
      "h-dvh",
      "min-h-0",
      "flex-col",
      "overflow-hidden",
    );
    expect(shell).not.toHaveClass("min-h-dvh");
    expect(masthead.parentElement).toBe(shell);
    expect(routeViewport?.parentElement).toBe(shell);
    expect(routeViewport).toHaveClass(
      "flex",
      "min-h-0",
      "w-full",
      "flex-1",
      "overflow-hidden",
    );
    expect(routeViewport).not.toHaveClass("min-h-dvh");
    expect(routeViewport?.className).not.toContain("calc(100dvh");
    expect(routeViewport?.className.split(/\s+/)).not.toEqual(
      expect.arrayContaining([
        expect.stringMatching(/^(?:[^:]+:)*(?:p|px|py|pt|pr|pb|pl)-/),
      ]),
    );
  });
});
