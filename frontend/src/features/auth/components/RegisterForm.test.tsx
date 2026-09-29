import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  return {
    signUpEmail: vi.fn(),
    router: {
      push: vi.fn(),
      replace: vi.fn(),
      refresh: vi.fn(),
      back: vi.fn(),
      forward: vi.fn(),
      prefetch: vi.fn(),
    },
  };
});

vi.mock("@/lib/auth/auth-client", () => ({
  signUp: { email: mocks.signUpEmail },
}));

vi.mock("next/navigation", () => ({
  useRouter: () => mocks.router,
}));

import { createRouterMock } from "@/test/router-mock";
import { RegisterForm } from "./RegisterForm";

beforeEach(() => {
  vi.clearAllMocks();
  Object.assign(mocks.router, createRouterMock());
});

const fillForm = async (
  user: ReturnType<typeof userEvent.setup>,
  email: string,
  password: string,
) => {
  await user.type(screen.getByLabelText("メールアドレス"), email);
  await user.type(screen.getByLabelText("パスワード"), password);
};

const submitButton = () =>
  screen.getByRole("button", { name: "アカウントを作成" });

describe("RegisterForm — 初期表示", () => {
  it("メールアドレスとパスワードだけの日本語フォームとログイン導線を表示する", () => {
    render(<RegisterForm />);

    expect(screen.getByRole("heading", { name: "新規登録" })).toBeVisible();
    expect(
      screen.getByText("メールアドレスとパスワードでアカウントを作成します"),
    ).toBeVisible();
    expect(screen.getByLabelText("メールアドレス")).toHaveAttribute(
      "autocomplete",
      "email",
    );
    expect(screen.getByLabelText("パスワード")).toHaveAttribute(
      "autocomplete",
      "new-password",
    );
    expect(screen.getByLabelText("パスワード")).toHaveAccessibleDescription(
      "8文字以上",
    );
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
    expect(submitButton()).toBeEnabled();
    expect(screen.getByRole("link", { name: "ログイン" })).toHaveAttribute(
      "href",
      "/auth/login",
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("RegisterForm — 入力検証", () => {
  it("空のまま送信すると両方の欄に日本語のエラーを出し、メール欄にフォーカスする", async () => {
    // jsdom は required / type=email の HTML5 検証を実装しているため submit を直接起こす。
    render(<RegisterForm />);
    const form = submitButton().closest("form");
    expect(form).not.toBeNull();
    if (form) fireEvent.submit(form);

    const emailInput = screen.getByLabelText("メールアドレス");
    const passwordInput = screen.getByLabelText("パスワード");
    await waitFor(() => {
      expect(emailInput).toHaveAttribute("aria-invalid", "true");
      expect(passwordInput).toHaveAttribute("aria-invalid", "true");
    });

    const alerts = screen.getAllByRole("alert");
    expect(alerts).toHaveLength(2);
    expect(alerts[0]).toHaveTextContent(
      "有効なメールアドレスを入力してください。",
    );
    expect(alerts[1]).toHaveTextContent(
      "パスワードは8文字以上で入力してください。",
    );
    expect(emailInput).toHaveFocus();
    expect(mocks.signUpEmail).not.toHaveBeenCalled();
  });

  it("パスワードだけが短いときはパスワード欄だけを invalid にしてフォーカスする", async () => {
    const user = userEvent.setup();
    render(<RegisterForm />);
    await fillForm(user, "user@example.com", "short12");
    await user.click(submitButton());

    const passwordInput = screen.getByLabelText("パスワード");
    await waitFor(() => {
      expect(passwordInput).toHaveAttribute("aria-invalid", "true");
    });
    expect(screen.getByRole("alert")).toHaveTextContent(
      "パスワードは8文字以上で入力してください。",
    );
    expect(screen.getByLabelText("メールアドレス")).not.toHaveAttribute(
      "aria-invalid",
    );
    expect(passwordInput).toHaveFocus();
    expect(mocks.signUpEmail).not.toHaveBeenCalled();
  });
});

describe("RegisterForm — signUp 呼び出し", () => {
  it("メールアドレスを小文字にし、name を空文字にして登録する", async () => {
    mocks.signUpEmail.mockResolvedValue({
      data: { user: { id: "u1" } },
      error: null,
    });

    const user = userEvent.setup();
    render(<RegisterForm />);
    await fillForm(user, "New.User@Example.COM", "password123");
    await user.click(submitButton());

    await waitFor(() => {
      expect(mocks.signUpEmail).toHaveBeenCalledWith({
        email: "new.user@example.com",
        password: "password123",
        name: "",
      });
    });
  });

  it.each([
    [
      "登録済みのメールアドレス",
      422,
      "USER_ALREADY_EXISTS_USE_ANOTHER_EMAIL",
      "メールアドレス",
      "このメールアドレスは登録済みです。",
    ],
    [
      "サーバーが拒否したメールアドレス",
      400,
      "INVALID_EMAIL",
      "メールアドレス",
      "有効なメールアドレスを入力してください。",
    ],
    [
      "サーバーが短いと判定したパスワード",
      400,
      "PASSWORD_TOO_SHORT",
      "パスワード",
      "パスワードは8文字以上で入力してください。",
    ],
    [
      "サーバーが長いと判定したパスワード",
      400,
      "PASSWORD_TOO_LONG",
      "パスワード",
      "パスワードは128文字以内で入力してください。",
    ],
  ])("%s は該当する欄のエラーとして表示し、入力値を保持する", async (_label, status, code, fieldLabel, expectedMessage) => {
    mocks.signUpEmail.mockResolvedValue({
      data: null,
      error: { status, statusText: "ignored", message: "ignored", code },
    });

    const user = userEvent.setup();
    render(<RegisterForm />);
    await fillForm(user, "user@example.com", "password123");
    await user.click(submitButton());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(expectedMessage);
    const field = screen.getByLabelText(fieldLabel);
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(field).toHaveFocus();
    expect(screen.getByLabelText("メールアドレス")).toHaveValue(
      "user@example.com",
    );
    expect(screen.getByLabelText("パスワード")).toHaveValue("password123");
    expect(mocks.router.push).not.toHaveBeenCalled();
  });

  it.each([
    [
      "試行制限 (429)",
      429,
      undefined,
      "登録の試行回数が上限に達しました。しばらくしてから再度お試しください。",
    ],
    [
      "原因を特定できない 422",
      422,
      "FAILED_TO_CREATE_USER",
      "登録に失敗しました。時間をおいて再度お試しください。",
    ],
    [
      "サーバー障害 (500)",
      500,
      undefined,
      "登録に失敗しました。時間をおいて再度お試しください。",
    ],
  ])("%s ならフォーム全体のエラーを出し、入力欄を invalid にしない", async (_label, status, code, expectedMessage) => {
    mocks.signUpEmail.mockResolvedValue({
      data: null,
      error: { status, statusText: "ignored", message: "ignored", code },
    });

    const user = userEvent.setup();
    render(<RegisterForm />);
    await fillForm(user, "user@example.com", "password123");
    await user.click(submitButton());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(expectedMessage);
    const emailInput = screen.getByLabelText("メールアドレス");
    const passwordInput = screen.getByLabelText("パスワード");
    expect(emailInput).not.toHaveAttribute("aria-invalid");
    expect(passwordInput).not.toHaveAttribute("aria-invalid");
    expect(emailInput).toHaveAccessibleDescription(expectedMessage);
    expect(emailInput).toHaveValue("user@example.com");
    expect(passwordInput).toHaveValue("password123");
    expect(submitButton()).toBeEnabled();
    expect(mocks.router.push).not.toHaveBeenCalled();
  });

  it("成功時に router.push('/') と router.refresh() を順序通り呼び、遷移まで送信中の表示を保つ", async () => {
    mocks.signUpEmail.mockResolvedValue({
      data: { user: { id: "u1" } },
      error: null,
    });

    const user = userEvent.setup();
    render(<RegisterForm />);
    await fillForm(user, "user@example.com", "password123");
    await user.click(submitButton());

    await waitFor(() => {
      expect(mocks.router.push).toHaveBeenCalledWith("/");
    });
    expect(mocks.router.refresh).toHaveBeenCalledTimes(1);
    const pushOrder = mocks.router.push.mock.invocationCallOrder[0] ?? 0;
    const refreshOrder = mocks.router.refresh.mock.invocationCallOrder[0] ?? 0;
    expect(pushOrder).toBeLessThan(refreshOrder);
    const busyButton = screen.getByRole("button", { name: "登録中…" });
    expect(busyButton).toBeDisabled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    await user.click(busyButton);
    expect(mocks.signUpEmail).toHaveBeenCalledTimes(1);
  });
});

describe("RegisterForm — pending state", () => {
  it("signUp 解決前は登録中の状態を伝え、入力値を保持して再送信できない", async () => {
    let resolveSignUp!: (v: { data: null; error: null }) => void;
    mocks.signUpEmail.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSignUp = resolve as typeof resolveSignUp;
        }),
    );

    const user = userEvent.setup();
    render(<RegisterForm />);
    await fillForm(user, "user@example.com", "password123");
    const emailInput = screen.getByLabelText("メールアドレス");
    const passwordInput = screen.getByLabelText("パスワード");
    await user.click(submitButton());

    const pendingButton = await screen.findByRole("button", {
      name: "登録中…",
    });
    expect(pendingButton).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("登録中…");
    expect(emailInput).toBeDisabled();
    expect(passwordInput).toBeDisabled();
    expect(emailInput).toHaveValue("user@example.com");
    expect(passwordInput).toHaveValue("password123");

    await user.click(pendingButton);
    expect(mocks.signUpEmail).toHaveBeenCalledTimes(1);

    resolveSignUp({ data: null, error: null });
  });
});
