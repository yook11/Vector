import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Server Action は **相対 path** で mock する。`@/features/watchlist/api/...` の
// deep path は biome `noRestrictedImports` で禁止されており、test での mock
// path も同じ規約に従う。
const mocks = vi.hoisted(() => ({
  addToWatchlist: vi.fn(),
  removeFromWatchlist: vi.fn(),
  toastError: vi.fn(),
}));

vi.mock("../api/add-to-watchlist", () => ({
  addToWatchlist: mocks.addToWatchlist,
}));

vi.mock("../api/remove-from-watchlist", () => ({
  removeFromWatchlist: mocks.removeFromWatchlist,
}));

vi.mock("@/lib/utils/toast-error", () => ({
  toastError: mocks.toastError,
}));

import { WatchlistButton } from "./WatchlistButton";

let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  vi.clearAllMocks();
  // reject test で console.error が走るので noisy log を抑制
  consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  consoleErrorSpy.mockRestore();
});

const getButton = () =>
  screen.getByRole("button", {
    name: /watchlist/i,
  });

describe("WatchlistButton — 初期表示", () => {
  it("isWatched=false で aria-pressed=false / Add ラベル / fill-current なし", () => {
    render(<WatchlistButton articleId={1} isWatched={false} />);
    const button = getButton();
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(button).toHaveAttribute("aria-label", "Add to watchlist");
    expect(button).toHaveAttribute("title", "Add to watchlist");
    // Bookmark icon に fill-current が付かないこと
    const icon = button.querySelector("svg");
    expect(icon).not.toBeNull();
    expect(icon?.classList.contains("fill-current")).toBe(false);
  });

  it("isWatched=true で aria-pressed=true / Remove ラベル / fill-current あり", () => {
    render(<WatchlistButton articleId={1} isWatched={true} />);
    const button = getButton();
    expect(button).toHaveAttribute("aria-pressed", "true");
    expect(button).toHaveAttribute("aria-label", "Remove from watchlist");
    expect(button).toHaveAttribute("title", "Remove from watchlist");
    const icon = button.querySelector("svg");
    expect(icon?.classList.contains("fill-current")).toBe(true);
  });
});

describe("WatchlistButton — 楽観的 toggle (mid-flight)", () => {
  it("click 直後に aria-pressed=true へ flip し addToWatchlist を呼ぶ", async () => {
    // Server Action を解決させずに保留して mid-flight の DOM を観察する
    let resolveAdd!: () => void;
    mocks.addToWatchlist.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          resolveAdd = resolve;
        }),
    );

    const user = userEvent.setup();
    render(<WatchlistButton articleId={42} isWatched={false} />);
    const button = getButton();

    await user.click(button);

    // optimistic state が即時反映 (transition 中)
    await waitFor(() => {
      expect(button).toHaveAttribute("aria-pressed", "true");
    });
    expect(button).toHaveAttribute("aria-label", "Remove from watchlist");
    expect(button).toBeDisabled();
    expect(mocks.addToWatchlist).toHaveBeenCalledTimes(1);
    expect(mocks.addToWatchlist).toHaveBeenCalledWith(42);
    expect(mocks.removeFromWatchlist).not.toHaveBeenCalled();

    resolveAdd();
  });

  it("isWatched=true から click すると removeFromWatchlist を呼び aria-pressed=false に flip", async () => {
    let resolveRemove!: () => void;
    mocks.removeFromWatchlist.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          resolveRemove = resolve;
        }),
    );

    const user = userEvent.setup();
    render(<WatchlistButton articleId={7} isWatched={true} />);
    const button = getButton();

    await user.click(button);

    await waitFor(() => {
      expect(button).toHaveAttribute("aria-pressed", "false");
    });
    expect(mocks.removeFromWatchlist).toHaveBeenCalledWith(7);
    expect(mocks.addToWatchlist).not.toHaveBeenCalled();

    resolveRemove();
  });
});

describe("WatchlistButton — 失敗時の挙動", () => {
  it("addToWatchlist が reject すると base state に revert + toastError + console.error", async () => {
    const error = new Error("Network down");
    mocks.addToWatchlist.mockRejectedValue(error);

    const user = userEvent.setup();
    render(<WatchlistButton articleId={1} isWatched={false} />);
    const button = getButton();

    await user.click(button);

    // 失敗後は startTransition 完了 → optimistic が base (false) へ自動 revert
    await waitFor(() => {
      expect(button).toHaveAttribute("aria-pressed", "false");
    });
    expect(button).toHaveAttribute("aria-label", "Add to watchlist");
    expect(mocks.toastError).toHaveBeenCalledTimes(1);
    expect(mocks.toastError).toHaveBeenCalledWith(
      error,
      "ウォッチリストへの追加に失敗しました",
    );
    expect(consoleErrorSpy).toHaveBeenCalledWith(
      "Watchlist toggle failed",
      error,
    );
  });

  it("removeFromWatchlist が reject すると revert + 削除用エラー文言で toastError", async () => {
    const error = new Error("Conflict");
    mocks.removeFromWatchlist.mockRejectedValue(error);

    const user = userEvent.setup();
    render(<WatchlistButton articleId={2} isWatched={true} />);
    const button = getButton();

    await user.click(button);

    await waitFor(() => {
      expect(button).toHaveAttribute("aria-pressed", "true");
    });
    expect(mocks.toastError).toHaveBeenCalledWith(
      error,
      "ウォッチリストから削除できませんでした",
    );
  });
});

describe("WatchlistButton — pending 中の disabled", () => {
  it("Server Action 解決前は button が disabled で連打を防ぐ", async () => {
    let resolveAdd!: () => void;
    mocks.addToWatchlist.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          resolveAdd = resolve;
        }),
    );

    const user = userEvent.setup();
    render(<WatchlistButton articleId={1} isWatched={false} />);
    const button = getButton();

    await user.click(button);
    await waitFor(() => {
      expect(button).toBeDisabled();
    });

    // 連打しても 2 度 addToWatchlist は呼ばれない
    await user.click(button);
    expect(mocks.addToWatchlist).toHaveBeenCalledTimes(1);

    resolveAdd();
  });
});
