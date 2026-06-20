import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  update: vi.fn(),
  isPending: false,
}));

vi.mock("@/lib/search-params/client", () => ({
  useUpdateSearchParams: () => ({
    updateSearchParams: mocks.update,
    isPending: mocks.isPending,
  }),
}));

import { PaperNewsPagination } from "./PaperNewsPagination";

// 可変 mock は vitest が自動リセットしないため、各テスト前に既定へ戻す。
beforeEach(() => {
  mocks.isPending = false;
  mocks.update.mockClear();
});

describe("PaperNewsPagination — 描画条件", () => {
  it("totalPages <= 1 のとき何も描画しない", () => {
    const { container } = render(
      <PaperNewsPagination page={1} totalPages={1} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("totalPages が 0 のとき何も描画しない", () => {
    const { container } = render(
      <PaperNewsPagination page={1} totalPages={0} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("totalPages >= 2 のとき nav を描画する", () => {
    render(<PaperNewsPagination page={1} totalPages={2} />);
    expect(
      screen.getByRole("navigation", { name: "ニュースページ" }),
    ).toBeInTheDocument();
  });
});

describe("PaperNewsPagination — ボタン押下によるページ遷移", () => {
  it("Next 押下で updateSearchParams({ page: String(page+1) }) が呼ばれる", async () => {
    mocks.isPending = false;
    mocks.update.mockClear();
    render(<PaperNewsPagination page={2} totalPages={5} />);
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(mocks.update).toHaveBeenCalledWith({ page: "3" });
  });

  it("Previous 押下で updateSearchParams({ page: String(page-1) }) が呼ばれる", async () => {
    mocks.isPending = false;
    mocks.update.mockClear();
    render(<PaperNewsPagination page={3} totalPages={5} />);
    await userEvent.click(screen.getByRole("button", { name: "Previous" }));
    expect(mocks.update).toHaveBeenCalledWith({ page: "2" });
  });

  it("page=2 で Previous 押下すると page: undefined（page 1 はパラメータ削除）", async () => {
    mocks.isPending = false;
    mocks.update.mockClear();
    render(<PaperNewsPagination page={2} totalPages={5} />);
    await userEvent.click(screen.getByRole("button", { name: "Previous" }));
    expect(mocks.update).toHaveBeenCalledWith({ page: undefined });
  });
});

describe("PaperNewsPagination — ボタン disabled 境界", () => {
  it("page=1 のとき Previous は disabled", () => {
    mocks.isPending = false;
    render(<PaperNewsPagination page={1} totalPages={3} />);
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next" })).not.toBeDisabled();
  });

  it("page=totalPages のとき Next は disabled", () => {
    mocks.isPending = false;
    render(<PaperNewsPagination page={3} totalPages={3} />);
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Previous" })).not.toBeDisabled();
  });

  it("page が中間のとき Prev/Next ともに enabled", () => {
    mocks.isPending = false;
    render(<PaperNewsPagination page={2} totalPages={4} />);
    expect(screen.getByRole("button", { name: "Previous" })).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "Next" })).not.toBeDisabled();
  });
});

describe("PaperNewsPagination — isPending による連打ガード", () => {
  it("isPending=true のとき Prev/Next 両方 disabled", () => {
    mocks.isPending = true;
    render(<PaperNewsPagination page={2} totalPages={5} />);
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });

  it("isPending=true のときスピナーが opacity-100 クラスを持つ", () => {
    mocks.isPending = true;
    const { container } = render(
      <PaperNewsPagination page={2} totalPages={5} />,
    );
    // SVG の className は SVGAnimatedString のため classList.contains で確認する
    const spinner = container.querySelector(
      '[aria-hidden="true"].animate-spin',
    );
    expect(spinner).not.toBeNull();
    expect(spinner?.classList.contains("opacity-100")).toBe(true);
  });

  it("isPending=false のときスピナーが opacity-0 クラスを持つ（layout-shift-free）", () => {
    mocks.isPending = false;
    const { container } = render(
      <PaperNewsPagination page={2} totalPages={5} />,
    );
    const spinner = container.querySelector(
      '[aria-hidden="true"].animate-spin',
    );
    expect(spinner).not.toBeNull();
    expect(spinner?.classList.contains("opacity-0")).toBe(true);
  });
});
