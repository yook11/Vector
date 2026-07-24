import { render, screen } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

const bootstrapGate = vi.hoisted(() => new Promise<never>(() => undefined));

vi.mock("next/font/google", () => ({
  Big_Shoulders: () => ({ variable: "font-wordmark" }),
  Newsreader: () => ({ variable: "font-display" }),
  Plus_Jakarta_Sans: () => ({ variable: "font-sans" }),
  Shippori_Mincho_B1: () => ({ variable: "font-serif" }),
  Zen_Kaku_Gothic_New: () => ({ variable: "font-gothic" }),
  Zen_Maru_Gothic: () => ({ variable: "font-maru" }),
}));

vi.mock("@/components/layout/NonceThemeProvider", () => ({
  NonceThemeProvider: () => {
    throw bootstrapGate;
  },
}));

vi.mock("@/components/layout/ClientGlobals", () => ({
  ClientGlobals: () => null,
}));

import RootLayout from "./layout";

function rootSuspenseSubtree(children: ReactNode): ReactNode {
  const documentTree = RootLayout({ children });
  const body = documentTree.props.children as ReactElement<{
    children: ReactNode;
  }>;
  return body.props.children;
}

describe("RootLayout bootstrap fallback", () => {
  it("nonce boundary の待機中に可視の中立fallbackだけを表示する", () => {
    render(rootSuspenseSubtree(<p>認証済みユーザー: Private Member</p>));

    expect(screen.getByText("画面を準備しています…")).toBeVisible();
    expect(screen.queryByText("認証済みユーザー: Private Member")).toBeNull();
    expect(document.body).not.toHaveTextContent("Private Member");
    expect(document.querySelector("script")).toBeNull();
  });
});
