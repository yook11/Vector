import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  signInEmail: vi.fn(),
  push: vi.fn(),
  refresh: vi.fn(),
}));

vi.mock("@/lib/auth/auth-client", () => ({
  signIn: { email: mocks.signInEmail },
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

import { LoginForm } from "./LoginForm";

beforeEach(() => {
  vi.clearAllMocks();
});

const fillForm = async (
  user: ReturnType<typeof userEvent.setup>,
  email: string,
  password: string,
) => {
  if (email !== "") {
    await user.type(screen.getByLabelText("Email"), email);
  }
  if (password !== "") {
    await user.type(screen.getByLabelText("Password"), password);
  }
};

describe("LoginForm — 初期表示", () => {
  it("error 表示なし、submit ボタン enabled", () => {
    render(<LoginForm />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    const button = screen.getByRole("button", { name: "Sign in" });
    expect(button).not.toBeDisabled();
  });
});

describe("LoginForm — クライアント検証失敗", () => {
  it("空フォーム submit で汎用 error 表示 + email focus + aria-invalid", async () => {
    render(<LoginForm />);
    // jsdom は HTML5 form validation (required / type=email) を実装している
    // ため `user.click(submitButton)` だと validation block が先に走り
    // handleSubmit に到達しない。fireEvent.submit(form) で直接 submit event
    // を投げて Zod 検証側を確実に通す。
    const form = screen
      .getByRole("button", { name: "Sign in" })
      .closest("form");
    expect(form).not.toBeNull();
    if (form) fireEvent.submit(form);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Please enter a valid email and password");
    expect(screen.getByLabelText("Email")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(screen.getByLabelText("Email")).toHaveFocus();
    // signIn は呼ばれない
    expect(mocks.signInEmail).not.toHaveBeenCalled();
  });
});

describe("LoginForm — signIn 戻り値", () => {
  it("authError があれば 'Invalid email or password' 表示 + email focus", async () => {
    mocks.signInEmail.mockResolvedValue({
      data: null,
      error: { message: "ignored", code: "INVALID_CREDENTIALS" },
    });

    const user = userEvent.setup();
    render(<LoginForm />);
    await fillForm(user, "user@example.com", "secret");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Invalid email or password");
    expect(screen.getByLabelText("Email")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(screen.getByLabelText("Email")).toHaveFocus();
    expect(mocks.signInEmail).toHaveBeenCalledWith({
      email: "user@example.com",
      password: "secret",
    });
    expect(mocks.push).not.toHaveBeenCalled();
    expect(mocks.refresh).not.toHaveBeenCalled();
  });

  it("成功時に router.push('/') と router.refresh() を順序通り呼ぶ", async () => {
    mocks.signInEmail.mockResolvedValue({
      data: { user: { id: "u1" } },
      error: null,
    });

    const user = userEvent.setup();
    render(<LoginForm />);
    await fillForm(user, "user@example.com", "secret");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => {
      expect(mocks.push).toHaveBeenCalledWith("/");
    });
    expect(mocks.refresh).toHaveBeenCalledTimes(1);
    // push が refresh より先に呼ばれていること
    const pushOrder = mocks.push.mock.invocationCallOrder[0] ?? 0;
    const refreshOrder = mocks.refresh.mock.invocationCallOrder[0] ?? 0;
    expect(pushOrder).toBeLessThan(refreshOrder);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("LoginForm — pending state", () => {
  it("signIn 解決前は button disabled + label 'Signing in…'", async () => {
    let resolveSign!: (v: { data: null; error: null }) => void;
    mocks.signInEmail.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSign = resolve as typeof resolveSign;
        }),
    );

    const user = userEvent.setup();
    render(<LoginForm />);
    await fillForm(user, "user@example.com", "secret");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Signing in…" }),
      ).toBeDisabled();
    });

    resolveSign({ data: null, error: null });
  });
});

describe("LoginForm — 再 submit で error クリア", () => {
  it("失敗後に再度 submit すると error 表示が一旦消える", async () => {
    mocks.signInEmail
      .mockResolvedValueOnce({
        data: null,
        error: { message: "fail" },
      })
      .mockImplementationOnce(
        () => new Promise(() => {}), // 解決させず error が消えた状態を観察
      );

    const user = userEvent.setup();
    render(<LoginForm />);
    await fillForm(user, "user@example.com", "secret");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByRole("alert");

    await user.click(screen.getByRole("button", { name: "Sign in" }));
    // setError(null) が submit 冒頭で走るので、2 回目の signIn 解決前に
    // alert が消える
    await waitFor(() => {
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });
  });
});
