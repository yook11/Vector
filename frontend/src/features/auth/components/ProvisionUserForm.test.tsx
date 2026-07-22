import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { passwordPolicy } from "@/lib/auth/auth-config";

const mocks = vi.hoisted(() => ({
  provisionUser: vi.fn(),
  writeText: vi.fn(),
}));

vi.mock("../api/provision-user", () => ({
  provisionUser: mocks.provisionUser,
}));

import { ProvisionUserForm } from "./ProvisionUserForm";

const EMAIL = "new-user@example.com";
const PASSWORD = "plain-password";

function nameInput(): HTMLElement {
  return screen.getByLabelText(/名前|氏名/);
}

function emailInput(): HTMLElement {
  return screen.getByLabelText("メールアドレス");
}

function passwordInput(): HTMLElement {
  return screen.getByLabelText("パスワード");
}

function confirmationCheckbox(): HTMLElement {
  return screen.getByRole("checkbox", { name: "認証情報を控えました" });
}

function submitButton(): HTMLElement {
  return screen.getByRole("button", { name: /登録/ });
}

function renderForm() {
  return render(<ProvisionUserForm />);
}

async function fillValidForm(
  user: ReturnType<typeof userEvent.setup>,
  values: { name?: string; email?: string; password?: string } = {},
) {
  await user.type(nameInput(), values.name ?? "山田 太郎");
  await user.type(emailInput(), values.email ?? EMAIL);
  await user.type(passwordInput(), values.password ?? PASSWORD);
}

function installClipboard(): () => void {
  const descriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText: mocks.writeText },
  });
  return () => {
    if (descriptor) {
      Object.defineProperty(navigator, "clipboard", descriptor);
    } else {
      Reflect.deleteProperty(navigator, "clipboard");
    }
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.provisionUser.mockResolvedValue({ status: "idle" });
  mocks.writeText.mockResolvedValue(undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ProvisionUserForm", () => {
  it("日本語の必要項目と確認ゲートだけを表示する", async () => {
    await renderForm();

    expect(nameInput()).toBeVisible();
    expect(emailInput()).toBeVisible();
    expect(passwordInput()).toBeVisible();
    expect(
      screen.getByRole("button", { name: "認証情報をコピー" }),
    ).toBeVisible();
    expect(confirmationCheckbox()).toBeVisible();
    expect(submitButton()).toBeDisabled();
    expect(screen.queryByLabelText(/ロール|role/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/ID|識別子/i)).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText(/プロバイダー|provider/i),
    ).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/確認用パスワード/)).not.toBeInTheDocument();
  });

  it("password input の長さ属性をshared policyに同期する", async () => {
    await renderForm();

    expect(passwordInput()).toHaveAttribute(
      "minlength",
      String(passwordPolicy.minLength),
    );
    expect(passwordInput()).toHaveAttribute(
      "maxlength",
      String(passwordPolicy.maxLength),
    );
  });

  it("控え確認を email/password の変更時だけ解除する", async () => {
    const user = userEvent.setup();
    await renderForm();
    await fillValidForm(user);

    await user.click(confirmationCheckbox());
    expect(confirmationCheckbox()).toBeChecked();
    expect(submitButton()).toBeEnabled();

    await user.type(nameInput(), " 次郎");
    expect(confirmationCheckbox()).toBeChecked();
    expect(submitButton()).toBeEnabled();

    await user.type(emailInput(), "+updated");
    expect(confirmationCheckbox()).not.toBeChecked();
    expect(submitButton()).toBeDisabled();

    await user.click(confirmationCheckbox());
    await user.type(passwordInput(), "-updated");
    expect(confirmationCheckbox()).not.toBeChecked();
    expect(submitButton()).toBeDisabled();
  });

  it("明示操作で現在の email/password だけをコピーし、失敗後も手動確認できる", async () => {
    const user = userEvent.setup();
    await renderForm();
    await fillValidForm(user, { name: "山田 次郎" });
    const restoreClipboard = installClipboard();
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});
    const consoleWarn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const consoleLog = vi.spyOn(console, "log").mockImplementation(() => {});
    const setItem = vi.spyOn(Storage.prototype, "setItem");

    try {
      await user.click(
        screen.getByRole("button", { name: "認証情報をコピー" }),
      );

      await waitFor(() => expect(mocks.writeText).toHaveBeenCalledOnce());
      const copyText = String(mocks.writeText.mock.calls[0]?.[0] ?? "");
      expect(copyText).toContain(EMAIL);
      expect(copyText).toContain(PASSWORD);
      expect(copyText).not.toContain("山田 次郎");
      expect(copyText).not.toContain("role");
      expect(screen.getByRole("status")).toHaveTextContent(/コピー/);
      expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");

      mocks.writeText.mockRejectedValueOnce(new Error("clipboard unavailable"));
      await user.click(
        screen.getByRole("button", { name: "認証情報をコピー" }),
      );
      expect(await screen.findByRole("alert")).toHaveTextContent(/コピー/);
      await user.click(confirmationCheckbox());
      expect(submitButton()).toBeEnabled();
      expect(consoleError).not.toHaveBeenCalled();
      expect(consoleWarn).not.toHaveBeenCalled();
      expect(consoleLog).not.toHaveBeenCalled();
      expect(setItem).not.toHaveBeenCalled();
    } finally {
      restoreClipboard();
      consoleError.mockRestore();
      consoleWarn.mockRestore();
      consoleLog.mockRestore();
      setItem.mockRestore();
    }
  });

  it("server action の pending 中は値を保持して全操作を無効化する", async () => {
    let resolveAction!: (state: { status: "success"; email: string }) => void;
    mocks.provisionUser.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveAction = resolve as typeof resolveAction;
        }),
    );
    const user = userEvent.setup();
    await renderForm();
    await fillValidForm(user);
    await user.click(confirmationCheckbox());
    await user.click(submitButton());

    const pendingStatus = await screen.findByRole("status");
    expect(pendingStatus).toHaveTextContent("登録中…");
    expect(pendingStatus).toHaveAttribute("aria-live", "polite");
    expect(nameInput()).toBeDisabled();
    expect(emailInput()).toBeDisabled();
    expect(passwordInput()).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "認証情報をコピー" }),
    ).toBeDisabled();
    expect(confirmationCheckbox()).toBeDisabled();
    expect(submitButton()).toBeDisabled();
    expect(emailInput()).toHaveValue(EMAIL);
    expect(passwordInput()).toHaveValue(PASSWORD);
    expect(mocks.provisionUser).toHaveBeenCalledOnce();

    resolveAction({ status: "success", email: EMAIL });
    expect(await screen.findByText("一般ユーザーを登録しました")).toBeVisible();
  });

  it("field/form エラー時も値と確認を保持し、支援技術へ伝える", async () => {
    mocks.provisionUser.mockResolvedValueOnce({
      status: "error",
      fieldErrors: {
        name: "名前を入力してください。",
        email: "有効なメールアドレスを入力してください。",
        password: "パスワードは8文字以上で入力してください。",
      },
      formError: "このメールアドレスは登録済みです。",
    });
    const user = userEvent.setup();
    await renderForm();
    await fillValidForm(user);
    await user.click(confirmationCheckbox());
    await user.click(submitButton());

    const formErrorText = await screen.findByText(
      "このメールアドレスは登録済みです。",
    );
    const formError = formErrorText.closest('[role="alert"]');
    expect(formError).not.toBeNull();
    if (!formError) {
      throw new Error(
        "form error は alert region 内に表示される必要があります。",
      );
    }
    expect(formError).toHaveAttribute("aria-live", "polite");
    for (const input of [nameInput(), emailInput(), passwordInput()]) {
      expect(input).toHaveAttribute("aria-invalid", "true");
      expect(input).toHaveAttribute("aria-describedby");
    }
    expect(screen.getByText("名前を入力してください。")).toBeVisible();
    expect(
      screen.getByText("有効なメールアドレスを入力してください。"),
    ).toBeVisible();
    expect(
      screen.getByText("パスワードは8文字以上で入力してください。"),
    ).toBeVisible();
    expect(emailInput()).toHaveValue(EMAIL);
    expect(passwordInput()).toHaveValue(PASSWORD);
    expect(confirmationCheckbox()).toBeChecked();
    expect(formError).not.toHaveTextContent(PASSWORD);
  });

  it("成功時に登録結果だけを表示し、入力・確認・コピー表示を消去する", async () => {
    mocks.provisionUser.mockResolvedValueOnce({
      status: "success",
      email: EMAIL,
    });
    const user = userEvent.setup();
    await renderForm();
    await fillValidForm(user);
    const restoreClipboard = installClipboard();

    try {
      await user.click(
        screen.getByRole("button", { name: "認証情報をコピー" }),
      );
      const copyFeedback = await screen.findByRole("status");
      await user.click(confirmationCheckbox());
      await user.click(submitButton());

      expect(
        await screen.findByText("一般ユーザーを登録しました"),
      ).toBeVisible();
      expect(screen.getByText(EMAIL)).toBeVisible();
      await waitFor(() => {
        expect(nameInput()).toHaveValue("");
        expect(emailInput()).toHaveValue("");
        expect(passwordInput()).toHaveValue("");
        expect(confirmationCheckbox()).not.toBeChecked();
        expect(copyFeedback).not.toBeInTheDocument();
      });
    } finally {
      restoreClipboard();
    }
  });
});
