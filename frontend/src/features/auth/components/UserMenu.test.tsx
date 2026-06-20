import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  signOut: vi.fn(),
  useSession: vi.fn(),
  toastError: vi.fn(),
}));

vi.mock("@/lib/auth/auth-client", () => ({
  signOut: mocks.signOut,
  useSession: mocks.useSession,
}));

vi.mock("@/lib/utils/toast-error", () => ({
  toastError: mocks.toastError,
}));

import { UserMenu } from "./UserMenu";

let consoleErrorSpy: ReturnType<typeof vi.spyOn>;
let originalLocation: Location;

beforeEach(() => {
  vi.clearAllMocks();
  mocks.useSession.mockReturnValue({ data: { user: { email: "a@b.com" } } });
  consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

  // jsdom では window.location.href への代入が例外になるため stub する
  originalLocation = window.location;
  Object.defineProperty(window, "location", {
    configurable: true,
    writable: true,
    value: { href: "" },
  });
});

afterEach(() => {
  consoleErrorSpy.mockRestore();
  Object.defineProperty(window, "location", {
    configurable: true,
    writable: true,
    value: originalLocation,
  });
});

const getButton = () => screen.getByRole("button");

describe("UserMenu — 初期表示", () => {
  it("session が null のとき null を返し何も描画しない", () => {
    mocks.useSession.mockReturnValue({ data: null });
    const { container } = render(<UserMenu />);
    expect(container.firstChild).toBeNull();
  });

  it("session.user が null のとき null を返す", () => {
    mocks.useSession.mockReturnValue({ data: { user: null } });
    const { container } = render(<UserMenu />);
    expect(container.firstChild).toBeNull();
  });

  it("session の email を表示する", () => {
    render(<UserMenu />);
    expect(screen.getByText("a@b.com")).toBeInTheDocument();
  });
});

describe("UserMenu — ログアウト成功", () => {
  it("signOut が { error: null } を返すと window.location.href を /auth/login に遷移し toastError を呼ばない", async () => {
    mocks.signOut.mockResolvedValue({ error: null });

    const user = userEvent.setup();
    render(<UserMenu />);
    await user.click(getButton());

    await waitFor(() => {
      expect(window.location.href).toBe("/auth/login");
    });
    expect(mocks.toastError).not.toHaveBeenCalled();
  });

  it("signOut が undefined を返すと window.location.href を /auth/login に遷移する", async () => {
    mocks.signOut.mockResolvedValue(undefined);

    const user = userEvent.setup();
    render(<UserMenu />);
    await user.click(getButton());

    await waitFor(() => {
      expect(window.location.href).toBe("/auth/login");
    });
    expect(mocks.toastError).not.toHaveBeenCalled();
  });
});

describe("UserMenu — ログアウト失敗(戻り値 error)", () => {
  it("signOut が { error: { message: 'x' } } を返すと toastError をフォールバック文言で呼び遷移しない", async () => {
    const error = { message: "Unauthorized" };
    mocks.signOut.mockResolvedValue({ error });

    const user = userEvent.setup();
    render(<UserMenu />);
    await user.click(getButton());

    await waitFor(() => {
      expect(mocks.toastError).toHaveBeenCalledTimes(1);
    });
    expect(mocks.toastError).toHaveBeenCalledWith(
      error,
      "ログアウトに失敗しました",
    );
    expect(window.location.href).toBe("");
  });
});

describe("UserMenu — ログアウト失敗(例外)", () => {
  it("signOut が reject すると toastError を呼び遷移しない", async () => {
    const error = new Error("Network error");
    mocks.signOut.mockRejectedValue(error);

    const user = userEvent.setup();
    render(<UserMenu />);
    await user.click(getButton());

    await waitFor(() => {
      expect(mocks.toastError).toHaveBeenCalledTimes(1);
    });
    expect(mocks.toastError).toHaveBeenCalledWith(
      error,
      "ログアウトに失敗しました",
    );
    expect(window.location.href).toBe("");
  });
});

describe("UserMenu — pending 中の disabled", () => {
  it("signOut 解決前はボタンが disabled かつ aria-busy=true になる", async () => {
    let resolveSignOut!: (v: { error: null }) => void;
    mocks.signOut.mockImplementation(
      () =>
        new Promise<{ error: null }>((resolve) => {
          resolveSignOut = resolve;
        }),
    );

    const user = userEvent.setup();
    render(<UserMenu />);
    const button = getButton();

    await user.click(button);

    await waitFor(() => {
      expect(button).toBeDisabled();
    });
    expect(button).toHaveAttribute("aria-busy", "true");

    // resolve 後は disabled が解除され遷移する
    resolveSignOut({ error: null });
    await waitFor(() => {
      expect(window.location.href).toBe("/auth/login");
    });
    // useTransition 完了後、ボタンは disabled 解除される
    // (遷移後の DOM は /auth/login への href 代入で変化しないが disabled は解除)
    expect(button).not.toBeDisabled();
  });
});
