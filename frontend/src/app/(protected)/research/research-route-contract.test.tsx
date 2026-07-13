import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ResearchThreadPage from "./[threadId]/page";
import ResearchPage from "./page";

const THREAD_ID = "00000000-0000-4000-a000-000000000001";

const mocks = vi.hoisted(() => ({
  parseLimit: vi.fn(),
  parseView: vi.fn(),
  getThreads: vi.fn(),
  loadThreadPage: vi.fn(),
  workspaceProps: [] as Record<string, unknown>[],
}));

vi.mock("@/features/research", () => ({
  getResearchThreads: mocks.getThreads,
  loadResearchThreadPage: mocks.loadThreadPage,
  parseResearchLimit: mocks.parseLimit,
  parseResearchView: mocks.parseView,
  ResearchUuidSchema: {
    safeParse: (value: string) => ({ success: true, data: value }),
  },
  ResearchWorkspace: (props: Record<string, unknown>) => {
    mocks.workspaceProps.push(props);
    return (
      <section data-testid="research-workspace">通常の回答workspace</section>
    );
  },
}));

vi.mock("@/components/layout/ShellMasthead", () => ({
  ShellMasthead: () => <header />,
}));

vi.mock("@/components/paper", () => ({
  PaperSurface: ({ children }: { children: ReactNode }) => (
    <div>{children}</div>
  ),
  PaperTexture: () => null,
}));

vi.mock("@/lib/auth/guards", () => ({
  requireSession: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("next/navigation", () => ({
  notFound: vi.fn(),
}));

beforeEach(() => {
  mocks.workspaceProps.length = 0;
  mocks.parseLimit.mockReset().mockReturnValue(2);
  mocks.parseView.mockReset().mockReturnValue({
    view: "sources",
    canonical: true,
  });
  mocks.getThreads.mockReset().mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    perPage: 2,
    totalPages: 0,
  });
  mocks.loadThreadPage.mockReset().mockResolvedValue({
    state: "ready",
    threads: {
      items: [],
      total: 0,
      page: 1,
      perPage: 2,
      totalPages: 0,
    },
    thread: {
      threadId: THREAD_ID,
      title: "Thread A",
      messages: [],
    },
  });
});

describe("Research route URL contract", () => {
  it.each([
    [
      "thread route",
      () =>
        ResearchThreadPage({
          params: Promise.resolve({ threadId: THREAD_ID }),
          searchParams: Promise.resolve({ limit: "2", view: "sources" }),
        }),
    ],
    [
      "new route",
      () =>
        ResearchPage({
          searchParams: Promise.resolve({ limit: "2", view: "sources" }),
        }),
    ],
  ])("%sは旧view queryを表示stateにせず通常workspaceを描画する", async (_label, page) => {
    render(await page());

    expect(screen.getByTestId("research-workspace")).toHaveTextContent(
      "通常の回答workspace",
    );
    expect(mocks.parseView).not.toHaveBeenCalled();
    expect(mocks.workspaceProps).toHaveLength(1);
    expect(mocks.workspaceProps[0]).not.toHaveProperty("initialView");
    expect(mocks.workspaceProps[0]).not.toHaveProperty("view");
  });
});
