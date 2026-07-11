import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ResearchComposer } from "./ResearchComposer";
import {
  ResearchNavigationBoundary,
  useResearchNavigation,
} from "./ResearchNavigationBoundary";

const mocks = vi.hoisted(() => ({
  push: vi.fn(),
  submit: vi.fn(),
  cancel: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/research/current",
  useSearchParams: () => new URLSearchParams(),
  useRouter: () => ({ push: mocks.push, refresh: vi.fn() }),
}));

vi.mock("../api/submit-research-question", () => ({
  submitResearchQuestion: mocks.submit,
}));

vi.mock("../api/cancel-research-run", () => ({
  cancelResearchRun: mocks.cancel,
}));

function StartNavigation() {
  const { navigate } = useResearchNavigation();
  return (
    <button
      type="button"
      onClick={() =>
        navigate({
          kind: "thread",
          href: "/research/target",
          threadId: "00000000-0000-4000-a000-000000000002",
          label: "Target",
        })
      }
    >
      navigation開始
    </button>
  );
}

function renderComposer(activeRunId: string | null = null) {
  return render(
    <ResearchNavigationBoundary sidebar={<aside>一覧</aside>}>
      <StartNavigation />
      <ResearchComposer threadId="current" activeRunId={activeRunId} />
    </ResearchNavigationBoundary>,
  );
}

beforeEach(() => {
  mocks.push.mockReset();
  mocks.submit.mockReset();
  mocks.cancel.mockReset();
});

describe("ResearchComposer navigation lock", () => {
  it("navigation pendingでtextareaとsendをdisabledにする", async () => {
    const user = userEvent.setup();
    renderComposer();
    const textarea = screen.getByRole("textbox", { name: "質問" });
    await user.type(textarea, "市場への影響は？");
    expect(screen.getByRole("button", { name: "送信" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "navigation開始" }));

    expect(textarea).toBeDisabled();
    expect(screen.getByRole("button", { name: "送信" })).toBeDisabled();
  });

  it("active run中はnavigation pendingでstopもdisabledにする", async () => {
    const user = userEvent.setup();
    renderComposer("00000000-0000-4000-a000-000000000099");
    expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "navigation開始" }));

    expect(screen.getByRole("textbox", { name: "質問" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
  });
});
