import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { SourceHealthLink } from "./SourceHealthLink";

describe("SourceHealthLink", () => {
  it("/admin/source-health への link を表示する", () => {
    render(<SourceHealthLink />);
    const link = screen.getByRole("link", { name: /source health/i });
    expect(link).toHaveAttribute("href", "/admin/source-health");
  });
});
