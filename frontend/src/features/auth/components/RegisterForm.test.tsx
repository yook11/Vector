import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  // createRouterMock と等価の shape を inline で生成。helper 自身は beforeEach
  // での再初期化に使い、3 ファイルで重複していた inline 定義を 1 箇所に集約。
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

describe("RegisterForm — schema fail (field-level error)", () => {
  it("password 8 文字未満で password field-level error + password focus (旧 bug regression)", async () => {
    const user = userEvent.setup();
    render(<RegisterForm />);
    await fillValidForm(user, { password: "short" });
    await user.click(screen.getByRole("button", { name: "Create account" }));

    // 旧コードは password 短すぎでも displayName ref に focus する bug があった。
    // 新実装では schema fail の field 順 (email > password > displayName) で
    // password が立つので passwordRef に focus する。
    await waitFor(() => {
      expect(screen.getByLabelText("Password")).toHaveFocus();
    });
    const passwordError = await screen.findByText(
      "Password must be at least 8 characters",
    );
    expect(passwordError).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    // email は invalid にならない (field-level a11y)
    expect(screen.getByLabelText("Email")).not.toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(mocks.signUpEmail).not.toHaveBeenCalled();
  });

  it("displayName が `<script>` で field-level error + displayName focus", async () => {
    const user = userEvent.setup();
    render(<RegisterForm />);
    await fillValidForm(user, { displayName: "<script>" });
    await user.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() => {
      expect(screen.getByLabelText("Display Name")).toHaveFocus();
    });
    expect(screen.getByLabelText("Display Name")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(mocks.signUpEmail).not.toHaveBeenCalled();
  });

  it("空 form submit で email field-level error + email focus", async () => {
    render(<RegisterForm />);
    const form = screen
      .getByRole("button", { name: "Create account" })
      .closest("form");
    expect(form).not.toBeNull();
    if (form) fireEvent.submit(form);

    await waitFor(() => {
      expect(screen.getByLabelText("Email")).toHaveFocus();
    });
    expect(screen.getByLabelText("Email")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
  });
});

describe("RegisterForm — i18n displayName", () => {
  it("日本語 displayName ('テックニュース') が schema を通り Better Auth に渡る", async () => {
    mocks.signUpEmail.mockResolvedValue({ data: { user: {} }, error: null });
    const user = userEvent.setup();
    render(<RegisterForm />);
    await fillValidForm(user, { displayName: "テックニュース" });
    await user.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() => {
      expect(mocks.signUpEmail).toHaveBeenCalledWith({
        email: "alice@example.com",
        password: "longenough",
        name: "テックニュース",
      });
    });
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
    expect(mocks.router.push).toHaveBeenCalledWith("/");
    expect(mocks.router.refresh).toHaveBeenCalled();
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

describe("RegisterForm — Better Auth error code → email field", () => {
  it.each([
    [
      "USER_ALREADY_EXISTS_USE_ANOTHER_EMAIL",
      "An account with this email already exists",
    ],
    ["USER_ALREADY_EXISTS", "An account with this email already exists"],
    ["INVALID_EMAIL", "Please enter a valid email address"],
  ] as const)("code=%s → email field error + email focus", async (code, expected) => {
    mocks.signUpEmail.mockResolvedValue({
      data: null,
      error: { code, status: 422 },
    });

    const user = userEvent.setup();
    render(<RegisterForm />);
    await fillValidForm(user);
    await user.click(screen.getByRole("button", { name: "Create account" }));

    const errorText = await screen.findByText(expected);
    expect(errorText).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByLabelText("Email")).toHaveFocus();
    });
    expect(screen.getByLabelText("Email")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(mocks.router.push).not.toHaveBeenCalled();
  });
});

describe("RegisterForm — Better Auth error code → password field (bug fix)", () => {
  it.each([
    ["PASSWORD_TOO_SHORT", "Password must be at least 8 characters"],
    ["PASSWORD_TOO_LONG", "Password is too long"],
  ] as const)("code=%s → password field error + password focus (旧 bug は displayName focus)", async (code, expected) => {
    mocks.signUpEmail.mockResolvedValue({
      data: null,
      error: { code, status: 422 },
    });

    const user = userEvent.setup();
    render(<RegisterForm />);
    await fillValidForm(user);
    await user.click(screen.getByRole("button", { name: "Create account" }));

    const errorText = await screen.findByText(expected);
    expect(errorText).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByLabelText("Password")).toHaveFocus();
    });
    expect(screen.getByLabelText("Password")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
  });
});

describe("RegisterForm — 未知 code", () => {
  it("status 422 + 未知 code → generic validation 文言 (formError) + email focus", async () => {
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
    await waitFor(() => {
      expect(screen.getByLabelText("Email")).toHaveFocus();
    });
  });

  it("status 500 + code 無し → generic failure 文言 + email focus", async () => {
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
    await waitFor(() => {
      expect(screen.getByLabelText("Email")).toHaveFocus();
    });
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

    const errorText = await screen.findByText(
      "An account with this email already exists",
    );
    expect(errorText).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByLabelText("Email")).toHaveFocus();
    });
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
