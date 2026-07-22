import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  router: {
    push: vi.fn(),
    refresh: vi.fn(),
  },
}));

vi.mock("next/server", () => ({ connection: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => mocks.router,
}));
vi.mock("server-only", () => ({}));

import RegisterPage from "./page";

describe("RegisterPage", () => {
  it("招待制の案内とログイン導線だけを表示し、公開登録フォームを出さない", async () => {
    const { container } = render(await RegisterPage());

    expect(screen.getByText("招待制で運用しています")).toBeVisible();
    expect(
      screen.getByText("現在、一般向けの新規登録は受け付けていません。"),
    ).toBeVisible();
    expect(
      screen.getByText("アカウントをお持ちの方はログインしてください。"),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: "ログイン" })).toHaveAttribute(
      "href",
      "/auth/login",
    );
    expect(container.querySelector("form")).toBeNull();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(container.querySelector('input[type="email"]')).toBeNull();
    expect(container.querySelector('input[type="password"]')).toBeNull();
    expect(
      screen.queryByRole("button", { name: "アカウントを作成" }),
    ).not.toBeInTheDocument();
  });
});
