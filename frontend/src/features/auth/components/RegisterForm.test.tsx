import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  signUpEmail: vi.fn(),
  push: vi.fn(),
  refresh: vi.fn(),
}));

vi.mock("@/lib/auth/auth-client", () => ({
  signUp: { email: mocks.signUpEmail },
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: mocks.push,
    refresh: mocks.refresh,
    replace: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    prefetch: vi.fn(),
  }),
}));

import { RegisterForm } from "./RegisterForm";

beforeEach(() => {
  vi.clearAllMocks();
});

const fillValidForm = async (
  user: ReturnType<typeof userEvent.setup>,
  overrides: Partial<{
    email: string;
    password: string;
    displayName: string;
  }> = {},
) => {
  const email = overrides.email ?? "alice@example.com";
  const password = overrides.password ?? "longenough";
  const displayName = overrides.displayName;
  if (displayName !== undefined && displayName !== "") {
    await user.type(screen.getByLabelText("Display Name"), displayName);
  }
  await user.type(screen.getByLabelText("Email"), email);
  await user.type(screen.getByLabelText("Password"), password);
};

describe("RegisterForm — 初期表示", () => {
  it("error 表示なし、Create account ボタン enabled", () => {
    render(<RegisterForm />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Create account" }),
    ).not.toBeDisabled();
  });
});

describe("RegisterForm — クライアント検証失敗", () => {
  it("password 8 文字未満で 'Please check your input and try again' + email focus", async () => {
    const user = userEvent.setup();
    render(<RegisterForm />);
    await fillValidForm(user, { password: "short" });
    await user.click(screen.getByRole("button", { name: "Create account" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Please check your input and try again");
    expect(screen.getByLabelText("Email")).toHaveFocus();
    expect(mocks.signUpEmail).not.toHaveBeenCalled();
  });
});

describe("RegisterForm — signUp 成功", () => {
  it("displayName 入力時は name に displayName を渡し、push + refresh", async () => {
    mocks.signUpEmail.mockResolvedValue({ data: { user: {} }, error: null });
    const user = userEvent.setup();
    render(<RegisterForm />);
    await fillValidForm(user, { displayName: "Alice" });
    await user.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() => {
      expect(mocks.signUpEmail).toHaveBeenCalledWith({
        email: "alice@example.com",
        password: "longenough",
        name: "Alice",
      });
    });
    expect(mocks.push).toHaveBeenCalledWith("/");
    expect(mocks.refresh).toHaveBeenCalled();
  });

  it("displayName 空のとき name は email の local part にフォールバック", async () => {
    mocks.signUpEmail.mockResolvedValue({ data: { user: {} }, error: null });
    const user = userEvent.setup();
    render(<RegisterForm />);
    await fillValidForm(user, { email: "bob@example.com" });
    await user.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() => {
      expect(mocks.signUpEmail).toHaveBeenCalledWith({
        email: "bob@example.com",
        password: "longenough",
        name: "bob",
      });
    });
  });
});

describe("RegisterForm — 既知 error code → email field focus", () => {
  it.each([
    [
      "USER_ALREADY_EXISTS_USE_ANOTHER_EMAIL",
      "An account with this email already exists",
    ],
    ["USER_ALREADY_EXISTS", "An account with this email already exists"],
    ["INVALID_EMAIL", "Please enter a valid email address"],
  ] as const)("code=%s → 文言と email focus", async (code, expected) => {
    mocks.signUpEmail.mockResolvedValue({
      data: null,
      error: { code, status: 422 },
    });

    const user = userEvent.setup();
    render(<RegisterForm />);
    await fillValidForm(user);
    await user.click(screen.getByRole("button", { name: "Create account" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(expected);
    expect(screen.getByLabelText("Email")).toHaveFocus();
    expect(mocks.push).not.toHaveBeenCalled();
  });
});

describe("RegisterForm — 既知 error code → displayName field focus", () => {
  it.each([
    ["PASSWORD_TOO_SHORT", "Password must be at least 8 characters"],
    ["PASSWORD_TOO_LONG", "Password is too long"],
  ] as const)("code=%s → 文言と displayName focus (email 以外の field)", async (code, expected) => {
    mocks.signUpEmail.mockResolvedValue({
      data: null,
      error: { code, status: 422 },
    });

    const user = userEvent.setup();
    render(<RegisterForm />);
    await fillValidForm(user);
    await user.click(screen.getByRole("button", { name: "Create account" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(expected);
    expect(screen.getByLabelText("Display Name")).toHaveFocus();
  });
});

describe("RegisterForm — 未知 code", () => {
  it("status 422 + 未知 code → generic validation 文言 + displayName focus", async () => {
    mocks.signUpEmail.mockResolvedValue({
      data: null,
      error: { code: "SOMETHING_NEW", status: 422 },
    });

    const user = userEvent.setup();
    render(<RegisterForm />);
    await fillValidForm(user);
    await user.click(screen.getByRole("button", { name: "Create account" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Please check your input and try again");
    expect(screen.getByLabelText("Display Name")).toHaveFocus();
  });

  it("status 500 + code 無し → generic failure 文言 + displayName focus", async () => {
    mocks.signUpEmail.mockResolvedValue({
      data: null,
      error: { status: 500 },
    });

    const user = userEvent.setup();
    render(<RegisterForm />);
    await fillValidForm(user);
    await user.click(screen.getByRole("button", { name: "Create account" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Registration failed. Please try again later.",
    );
    expect(screen.getByLabelText("Display Name")).toHaveFocus();
  });

  it("`error.error.code` ネストでも既知 code を抽出する", async () => {
    mocks.signUpEmail.mockResolvedValue({
      data: null,
      error: { error: { code: "USER_ALREADY_EXISTS" }, status: 422 },
    });

    const user = userEvent.setup();
    render(<RegisterForm />);
    await fillValidForm(user);
    await user.click(screen.getByRole("button", { name: "Create account" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "An account with this email already exists",
    );
    expect(screen.getByLabelText("Email")).toHaveFocus();
  });
});

describe("RegisterForm — pending state", () => {
  it("signUp 解決前は button disabled + label 'Creating account…'", async () => {
    let resolveSignUp!: (v: { data: null; error: null }) => void;
    mocks.signUpEmail.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSignUp = resolve as typeof resolveSignUp;
        }),
    );

    const user = userEvent.setup();
    render(<RegisterForm />);
    await fillValidForm(user);
    await user.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Creating account…" }),
      ).toBeDisabled();
    });

    resolveSignUp({ data: null, error: null });
  });
});
