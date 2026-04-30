import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  // createRouterMock と等価の shape を inline で生成。vi.hoisted は top-level
  // に巻き上げられるため import を持ち込むと循環を起こしやすく、router-mock の
  // helper は beforeEach での再初期化に使う形で取り回す。
  return {
    signInEmail: vi.fn(),
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
  signIn: { email: mocks.signInEmail },
}));

vi.mock("next/navigation", () => ({
  useRouter: () => mocks.router,
}));

import { createRouterMock } from "@/test/router-mock";
import { LoginForm } from "./LoginForm";

beforeEach(() => {
  vi.clearAllMocks();
  // helper で都度新しい vi.fn 一式を組み立て、前 test の invocation history を
  // 持ち越さない。3 ファイルで重複していた inline 定義を 1 箇所 (router-mock.ts)
  // に集約。
  Object.assign(mocks.router, createRouterMock());
});

const fillForm = async (
  user: ReturnType<typeof userEvent.setup>,
  email: string,
  password: string,
) => {
  await user.type(screen.getByLabelText("Email"), email);
  await user.type(screen.getByLabelText("Password"), password);
};

describe("LoginForm — 初期表示", () => {
  it("error 表示なし、submit ボタン enabled", () => {
    render(<LoginForm />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    const button = screen.getByRole("button", { name: "Sign in" });
    expect(button).not.toBeDisabled();
  });
});

describe("LoginForm — schema fail (field-level error)", () => {
  it("空 email + 空 password で field 個別の error 文言 + email field のみ aria-invalid", async () => {
    // jsdom は HTML5 form validation (required / type=email) を実装しているため
    // user.click だと validation block が submit を止める。fireEvent.submit で
    // useActionState の action に到達させ、zod 経由の field error を観察する。
    render(<LoginForm />);
    const form = screen
      .getByRole("button", { name: "Sign in" })
      .closest("form");
    expect(form).not.toBeNull();
    if (form) fireEvent.submit(form);

    // 両 field に個別の field-level error が出る (旧実装の generic 文言ではない)
    const emailInput = screen.getByLabelText("Email");
    const passwordInput = screen.getByLabelText("Password");

    await waitFor(() => {
      expect(emailInput).toHaveAttribute("aria-invalid", "true");
      expect(passwordInput).toHaveAttribute("aria-invalid", "true");
    });

    // 両 field に個別の <p role="alert"> が出る
    const alerts = screen.getAllByRole("alert");
    expect(alerts.length).toBe(2);
    expect(alerts[0]).toHaveTextContent("Please enter a valid email address");
    expect(alerts[1]).toHaveTextContent("Password is required");

    // signIn は呼ばれない
    expect(mocks.signInEmail).not.toHaveBeenCalled();
  });

  it("invalid email + valid password で email field のみ aria-invalid + email error", async () => {
    const user = userEvent.setup();
    render(<LoginForm />);
    // type=email を満たす形 (`@.` を含む) で submit して HTML5 validation を通し、
    // zod email validator のみが弾く形にする。
    await fillForm(user, "user@bad", "anypw");
    const form = screen
      .getByRole("button", { name: "Sign in" })
      .closest("form");
    if (form) fireEvent.submit(form);

    await waitFor(() => {
      expect(screen.getByLabelText("Email")).toHaveAttribute(
        "aria-invalid",
        "true",
      );
    });
    // password は invalid にならない (field-level a11y の意図)
    expect(screen.getByLabelText("Password")).not.toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(mocks.signInEmail).not.toHaveBeenCalled();
  });
});

describe("LoginForm — signIn 戻り値", () => {
  it("authError があれば formError 表示 + 両 input が aria-invalid + email focus", async () => {
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
    // formError は credential 全体不正の意味なので両 input を invalid にする
    expect(screen.getByLabelText("Email")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(screen.getByLabelText("Password")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(screen.getByLabelText("Email")).toHaveFocus();
    expect(mocks.signInEmail).toHaveBeenCalledWith({
      email: "user@example.com",
      password: "secret",
    });
    expect(mocks.router.push).not.toHaveBeenCalled();
    expect(mocks.router.refresh).not.toHaveBeenCalled();
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
      expect(mocks.router.push).toHaveBeenCalledWith("/");
    });
    expect(mocks.router.refresh).toHaveBeenCalledTimes(1);
    const pushOrder = mocks.router.push.mock.invocationCallOrder[0] ?? 0;
    const refreshOrder = mocks.router.refresh.mock.invocationCallOrder[0] ?? 0;
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
