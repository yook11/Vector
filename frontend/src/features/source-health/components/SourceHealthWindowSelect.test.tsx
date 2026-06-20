import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ update: vi.fn(), isPending: false }));
vi.mock("@/lib/search-params/client", () => ({
  useUpdateSearchParams: () => ({
    updateSearchParams: mocks.update,
    isPending: mocks.isPending,
  }),
}));

import { SourceHealthWindowSelect } from "./SourceHealthWindowSelect";

describe("SourceHealthWindowSelect", () => {
  // 可変 mock は vitest が自動リセットしないため、各テスト前に既定へ戻す。
  beforeEach(() => {
    mocks.isPending = false;
  });

  it("現在の window を表示するトリガを描画する", () => {
    render(<SourceHealthWindowSelect current="48h" />);
    const trigger = screen.getByRole("combobox", { name: "表示期間" });
    expect(trigger).toBeInTheDocument();
    expect(trigger).toHaveTextContent("48h");
  });

  it("isPending=true のとき combobox trigger が disabled になる", () => {
    mocks.isPending = true;
    render(<SourceHealthWindowSelect current="24h" />);
    const trigger = screen.getByRole("combobox", { name: "表示期間" });
    expect(trigger).toBeDisabled();
  });
});
