import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import type {
  PaginatedResearchThreadResponse,
  ResearchThreadDetail,
} from "@/types/types.gen";

vi.mock("@/features/research/components/ResearchWorkspace", () => ({
  ResearchWorkspace: ({ thread }: { thread: ResearchThreadDetail | null }) => (
    <main data-testid="retained-research-workspace">
      {thread?.title ?? "新しいリサーチ"}
    </main>
  ),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn() }),
}));

import {
  ResearchRouteHost,
  ResearchRouteModelCommit,
  ResearchRouteRejectedOutcome,
} from "@/features/research-client";

const THREAD_ID = "00000000-0000-4000-a000-000000000001";
const THREADS = {
  items: [],
  total: 0,
  page: 1,
  perPage: 20,
  totalPages: 0,
} satisfies PaginatedResearchThreadResponse;
const THREAD = {
  threadId: THREAD_ID,
  title: "Retained thread",
  messages: [],
} satisfies ResearchThreadDetail;

function ReadyRoute() {
  return (
    <ResearchRouteModelCommit limit={20} thread={THREAD} threads={THREADS} />
  );
}

function Host({ children }: { children: ReactNode }) {
  return (
    <ResearchRouteHost initialFallback={<p>初期fallback</p>}>
      {children}
    </ResearchRouteHost>
  );
}

describe("ResearchRouteHost rejected route outcome", () => {
  it.each([
    [
      "not-found",
      "route-not-found",
      <section key="not-found" data-testid="route-not-found">
        <ResearchRouteRejectedOutcome />
        見つかりません
      </section>,
    ],
    [
      "error",
      "route-error",
      <section key="error" role="alert" data-testid="route-error">
        <ResearchRouteRejectedOutcome />
        読み込みに失敗しました
      </section>,
    ],
  ])("retained workspaceを%s outcomeと同時表示しない", async (_label, outcomeTestId, outcome) => {
    const view = render(
      <Host>
        <ReadyRoute />
      </Host>,
    );
    await waitFor(() =>
      expect(
        screen.getByTestId("retained-research-workspace"),
      ).toHaveTextContent("Retained thread"),
    );

    view.rerender(<Host>{outcome}</Host>);

    expect(screen.getByTestId(outcomeTestId)).toBeInTheDocument();
    expect(screen.queryByTestId("retained-research-workspace")).toBeNull();
  });
});
