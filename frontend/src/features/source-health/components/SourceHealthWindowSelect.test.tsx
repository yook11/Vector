import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ update: vi.fn() }));
vi.mock("@/lib/search-params/client", () => ({
  useUpdateSearchParams: () => mocks.update,
}));

import { SourceHealthWindowSelect } from "./SourceHealthWindowSelect";

describe("SourceHealthWindowSelect", () => {
  it("現在の window を表示するトリガを描画する", () => {
    render(<SourceHealthWindowSelect current="48h" />);
    const trigger = screen.getByRole("combobox", { name: "表示期間" });
    expect(trigger).toBeInTheDocument();
    expect(trigger).toHaveTextContent("48h");
  });
});
