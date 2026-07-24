import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import ResearchThreadPage from "./[threadId]/page";
import ResearchPage from "./page";

vi.mock("@/components/layout/ShellMasthead", () => ({
  ShellMasthead: () => <header data-testid="shell-masthead" />,
}));

vi.mock("@/components/paper", () => ({
  PaperSurface: ({ children }: { children: ReactNode }) => (
    <div data-testid="paper-surface">{children}</div>
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
}));

vi.mock("@/features/research-client", () => ({
  ResearchRouteModelCommit: () => <section data-testid="research-workspace" />,
}));

vi.mock("server-only", () => ({}));

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

describe.each(routes)("$name route content", ({ renderRoute }) => {
  it("shared masthead and workspace frameを重複して描画しない", async () => {
    render(await renderRoute());

    expect(screen.getByTestId("research-workspace")).toBeInTheDocument();
    expect(screen.queryByTestId("paper-surface")).not.toBeInTheDocument();
    expect(screen.queryByTestId("shell-masthead")).not.toBeInTheDocument();
    expect(screen.queryByTestId("paper-texture")).not.toBeInTheDocument();
  });
});
