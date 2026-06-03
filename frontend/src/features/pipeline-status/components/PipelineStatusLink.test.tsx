import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PipelineStatusLink } from "./PipelineStatusLink";

describe("PipelineStatusLink", () => {
  it("/admin/pipeline-status への link を表示する", () => {
    render(<PipelineStatusLink />);
    const link = screen.getByRole("link", { name: /pipeline status/i });
    expect(link).toHaveAttribute("href", "/admin/pipeline-status");
  });
});
