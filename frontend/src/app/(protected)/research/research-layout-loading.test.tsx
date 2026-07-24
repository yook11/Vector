import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

vi.mock("@/components/layout/ShellMasthead", () => ({
  ShellMasthead: () => <header data-testid="shell-masthead" />,
}));

vi.mock("@/components/layout/PageNavigation", () => ({
  PageNavigationContent: ({ children }: { children: ReactNode }) => (
    <div data-testid="page-navigation-content">{children}</div>
  ),
}));

vi.mock("@/components/paper", () => ({
  PaperSurface: ({ children }: { children: ReactNode }) => (
    <div data-testid="paper-surface">{children}</div>
  ),
  PaperTexture: () => <div data-testid="paper-texture" />,
}));

vi.mock("@/lib/auth/guards", () => ({
  requireSession: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("@/features/research-client", () => ({
  ResearchRouteHost: ({
    children,
    initialFallback,
  }: {
    children: ReactNode;
    initialFallback: ReactNode;
  }) => (
    <div data-testid="research-route-host">
      {initialFallback}
      {children}
    </div>
  ),
}));

function layoutExists(): boolean {
  return existsSync(layoutPath());
}

function layoutPath(): string {
  return resolve(process.cwd(), "src/app/(protected)/research/layout.tsx");
}

function PendingRoute(): never {
  throw new Promise(() => undefined);
}

type ResearchLayout = (props: {
  children: ReactNode;
}) => ReactNode | Promise<ReactNode>;

describe("Research initial loading shell", () => {
  it("keeps the shared frame, but exposes only a private-data-free workspace fallback", async () => {
    expect(layoutExists()).toBe(true);
    if (!layoutExists()) {
      return;
    }

    const layoutUrl = pathToFileURL(layoutPath()).href;
    const module = (await import(/* @vite-ignore */ layoutUrl)) as {
      default: ResearchLayout;
    };
    const tree = await module.default({ children: <PendingRoute /> });
    const { container } = render(tree);

    expect(screen.getByTestId("paper-surface")).toBeInTheDocument();
    const masthead = screen.getByTestId("shell-masthead");
    const pageNavigationContent = screen.getByTestId("page-navigation-content");
    expect(masthead).toBeInTheDocument();

    const status = screen.getByRole("status", {
      name: "Researchを読み込み中…",
    });
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveAttribute("aria-atomic", "true");
    expect(status).not.toHaveClass("sr-only");

    const skeleton = container.querySelector<HTMLElement>(
      "[aria-hidden='true']",
    );
    expect(skeleton).toBeInTheDocument();
    expect(skeleton?.className).toContain("motion-reduce:animate-none");
    expect(pageNavigationContent).toContainElement(skeleton);
    expect(masthead).not.toContainElement(pageNavigationContent);

    const workspaceGrid = skeleton?.querySelector<HTMLElement>(".grid");
    const sidebar = workspaceGrid?.querySelector<HTMLElement>("aside");
    expect(workspaceGrid).toHaveClass(
      "grid-cols-1",
      "lg:grid-cols-[15rem_minmax(0,1fr)]",
    );
    expect(workspaceGrid).not.toHaveClass("grid-cols-[15rem_minmax(0,1fr)]");
    expect(sidebar).toHaveClass("hidden", "lg:block");
    expect(
      screen.queryByText(/Thread A|通常の回答workspace/),
    ).not.toBeInTheDocument();
  });
});
