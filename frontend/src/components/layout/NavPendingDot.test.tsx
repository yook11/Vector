import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const useLinkStatusMock = vi.fn();
vi.mock("next/link", () => ({
  useLinkStatus: () => useLinkStatusMock(),
}));

import { NavPendingDot } from "./NavPendingDot";

describe("NavPendingDot", () => {
  it("stays hidden but mounted when navigation is not pending", () => {
    useLinkStatusMock.mockReturnValue({ pending: false });
    const { container } = render(<NavPendingDot />);
    const dot = container.firstChild as HTMLElement;

    expect(dot).toBeInTheDocument();
    expect(dot).toHaveClass("opacity-0");
    expect(dot).toHaveAttribute("aria-hidden", "true");
    expect(dot).not.toHaveAttribute("data-pending");
  });

  it("becomes visible while navigation is pending", () => {
    useLinkStatusMock.mockReturnValue({ pending: true });
    const { container } = render(<NavPendingDot />);
    const dot = container.firstChild as HTMLElement;

    expect(dot).toHaveClass("opacity-100");
    expect(dot).toHaveAttribute("data-pending", "");
  });
});
