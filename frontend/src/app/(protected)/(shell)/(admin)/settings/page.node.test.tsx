import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  PipelineStatusLink: vi.fn(() => "pipeline-status-link"),
  ProvisionUserLink: vi.fn(() => "provision-user-link"),
  SourceHealthLink: vi.fn(() => "source-health-link"),
  SourceManager: vi.fn(() => "source-manager"),
  getSources: vi.fn(),
  requireAdmin: vi.fn(),
}));

vi.mock("@/features/auth", () => ({
  ProvisionUserLink: mocks.ProvisionUserLink,
}));
vi.mock("@/features/pipeline-status", () => ({
  PipelineStatusLink: mocks.PipelineStatusLink,
}));
vi.mock("@/features/source-health", () => ({
  SourceHealthLink: mocks.SourceHealthLink,
}));
vi.mock("@/features/sources", () => ({
  getSources: mocks.getSources,
  SourceManager: mocks.SourceManager,
}));
vi.mock("@/lib/auth/guards", () => ({ requireAdmin: mocks.requireAdmin }));

import SettingsPage from "./page";

beforeEach(() => {
  vi.clearAllMocks();
  mocks.getSources.mockResolvedValue({ items: [] });
  mocks.requireAdmin.mockResolvedValue(undefined);
});

describe("SettingsPage", () => {
  it("ProvisionUserLink を合成し、既存の Pipeline/Source 導線を維持する", () => {
    const markup = renderToStaticMarkup(<SettingsPage />);

    expect(markup).toContain("provision-user-link");
    expect(markup).toContain("pipeline-status-link");
    expect(markup).toContain("source-health-link");
    expect(mocks.ProvisionUserLink).toHaveBeenCalledOnce();
    expect(mocks.PipelineStatusLink).toHaveBeenCalledOnce();
    expect(mocks.SourceHealthLink).toHaveBeenCalledOnce();
  });
});
