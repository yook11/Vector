import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// sonner の toast.error は副作用 (UI 表示) なので vi.mock で差し替え、
// 呼び出し引数のみを assertion する。features 横断 module ではなく
// 第三者ライブラリのため CLAUDE.md の features 横断 mock 禁止には抵触しない。
// vi.mock factory は top-level に hoist されるため、参照する変数は
// vi.hoisted 内で定義する必要がある。
const mocks = vi.hoisted(() => ({
  toastError: vi.fn(),
}));

vi.mock("sonner", () => ({
  toast: { error: mocks.toastError },
}));

import { toastError } from "./toast-error";

beforeEach(() => {
  mocks.toastError.mockReset();
});

describe("toastError — production マスク文言", () => {
  // React 19 / Next.js 16 が production build で Server Action throw の
  // error.message を以下文言でマスクする。これがそのまま toast に出ると
  // UX を損なうので、先頭一致で検出して fallback に置換する。security
  // 観点では「内部例外メッセージを UI に流出させない」ことが目的。
  const REACT_PROD_MASK_FULL =
    "An error occurred in the Server Components render. The specific message is omitted in production builds to avoid leaking sensitive details. A digest property is included on this error instance which may provide additional details about the nature of the error.";

  it("React production マスク文言は fallback に置換される (内部詳細を流出させない)", () => {
    toastError(new Error(REACT_PROD_MASK_FULL), "Failed to save changes");

    expect(mocks.toastError).toHaveBeenCalledTimes(1);
    expect(mocks.toastError).toHaveBeenCalledWith("Failed to save changes");
    // mask 文言は決して toast に流れない
    expect(mocks.toastError).not.toHaveBeenCalledWith(
      expect.stringContaining("specific message is omitted"),
    );
  });

  it("マスク prefix のみ (短縮形) でも fallback に置換", () => {
    // prefix `An error occurred in the Server` は production / experimental
    // build で文言バリエーションが将来増える可能性があるため、startsWith で
    // 緩く判定している。短縮形でも必ずマスクが効くことを担保。
    toastError(
      new Error("An error occurred in the Server (digest: abc)"),
      "Operation failed",
    );

    expect(mocks.toastError).toHaveBeenCalledWith("Operation failed");
  });

  it("自前の Error.message はマスクされず、そのまま表示される (dev / 自前 throw)", () => {
    toastError(new Error("Email already in use"), "Registration failed");

    expect(mocks.toastError).toHaveBeenCalledTimes(1);
    expect(mocks.toastError).toHaveBeenCalledWith("Email already in use");
  });
});

describe("toastError — Error 以外の入力", () => {
  it("Error インスタンスでない値 (string) は fallback に置換", () => {
    toastError("just a string", "Fallback message");
    expect(mocks.toastError).toHaveBeenCalledWith("Fallback message");
  });

  it("null は fallback に置換", () => {
    toastError(null, "Fallback for null");
    expect(mocks.toastError).toHaveBeenCalledWith("Fallback for null");
  });

  it("undefined は fallback に置換", () => {
    toastError(undefined, "Fallback for undefined");
    expect(mocks.toastError).toHaveBeenCalledWith("Fallback for undefined");
  });

  it("plain object ({ message }) は Error ではないので fallback に置換", () => {
    toastError({ message: "looks like Error" }, "Fallback for object");
    expect(mocks.toastError).toHaveBeenCalledWith("Fallback for object");
  });

  it("Error だが message が空文字列の場合は fallback に置換", () => {
    // err.message が空だと toast.error("") が表示されてしまうため fallback
    // に置換する (空 toast UX 回避)。
    toastError(new Error(""), "Fallback for empty message");
    expect(mocks.toastError).toHaveBeenCalledWith("Fallback for empty message");
  });
});

afterEach(() => {
  // vi.mock は module-level のため reset 不要だが、各 test 後の状態を明示する。
  mocks.toastError.mockClear();
});
