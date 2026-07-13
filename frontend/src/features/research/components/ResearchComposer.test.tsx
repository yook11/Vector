import { render, screen, within } from "@testing-library/react";
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

function renderComposer(
  activeRunId: string | null = null,
  threadId: string | undefined = "current",
) {
  return render(
    <ResearchNavigationBoundary sidebar={<aside>一覧</aside>}>
      <StartNavigation />
      <ResearchComposer threadId={threadId} activeRunId={activeRunId} />
    </ResearchNavigationBoundary>,
  );
}

function composerForm(): HTMLFormElement {
  const form = screen.getByRole("textbox", { name: "質問" }).closest("form");
  if (!(form instanceof HTMLFormElement)) {
    throw new Error("composer form is missing");
  }
  return form;
}

function composerRail(form: HTMLFormElement): HTMLElement {
  const rails = Array.from(form.children).filter(
    (child): child is HTMLElement =>
      child instanceof HTMLElement &&
      child.classList.contains("w-full") &&
      child.classList.contains("max-w-[860px]") &&
      child.classList.contains("mx-auto"),
  );
  expect(rails).toHaveLength(1);
  const rail = rails[0];
  if (rail === undefined) throw new Error("composer rail is missing");
  return rail;
}

function expectTouchTarget(button: HTMLElement): void {
  expect(button.className).toMatch(
    /(?:^|\s)(?:min-h-|h-|size-)(?:11|12|14|16|\[44px\])(?:\s|$)/,
  );
}

beforeEach(() => {
  mocks.push.mockReset();
  mocks.submit.mockReset();
  mocks.cancel.mockReset();
});

describe("ResearchComposer dock contract", () => {
  it("formをshrinkしない非scroll dockとして通常flowに置く", () => {
    renderComposer();

    const form = composerForm();
    expect(form).toHaveClass("shrink-0");
    expect(form).not.toHaveClass(
      "fixed",
      "absolute",
      "sticky",
      "overflow-y-auto",
    );
  });

  it.each([
    ["empty", undefined],
    ["thread", "current"],
  ])("%s composerはanswerと同じinner railへ入力controlを収める", (_state, threadId) => {
    renderComposer(null, threadId);

    const form = composerForm();
    const rail = composerRail(form);
    expect(form).not.toHaveClass("max-w-[860px]", "mx-auto");
    expect(within(rail).getByRole("textbox", { name: "質問" })).toBe(
      screen.getByRole("textbox", { name: "質問" }),
    );
    expect(within(rail).getByRole("button", { name: "送信" })).toBe(
      screen.getByRole("button", { name: "送信" }),
    );
  });

  it("active runのcancel controlも同じinner railに保つ", () => {
    renderComposer("00000000-0000-4000-a000-000000000099");

    const rail = composerRail(composerForm());
    expect(within(rail).getByRole("button", { name: "停止" })).toBe(
      screen.getByRole("button", { name: "停止" }),
    );
  });

  it("通常paddingとsafe-area insetを明示的に合成する", () => {
    renderComposer();

    const form = composerForm();
    const paddingContract = `${form.className} ${form.style.paddingBottom}`;
    expect(paddingContract).toContain("calc(");
    expect(paddingContract).toMatch(/\d+(?:\.\d+)?(?:rem|px)/);
    expect(paddingContract).toContain("env(safe-area-inset-bottom)");
  });

  it("accessibleなnative textareaの入力contractを維持する", () => {
    renderComposer();

    const textarea = screen.getByRole<HTMLTextAreaElement>("textbox", {
      name: "質問",
    });
    expect(textarea.tagName).toBe("TEXTAREA");
    expect(textarea.getAttribute("name")?.trim().length ?? 0).toBeGreaterThan(
      0,
    );
    expect(textarea).toHaveAttribute("rows", "2");
    expect(textarea).toHaveAttribute("maxlength", "1000");
  });

  it("mobileで16px以上の入力文字と44px以上のsubmit targetを持つ", () => {
    renderComposer();

    const textarea = screen.getByRole("textbox", { name: "質問" });
    expect(textarea).toHaveClass("text-base");
    expect(textarea).not.toHaveClass("text-sm");
    expectTouchTarget(screen.getByRole("button", { name: "送信" }));
  });

  it("active runのcancel controlも44px以上のtouch targetを持つ", () => {
    renderComposer("00000000-0000-4000-a000-000000000099");

    expectTouchTarget(screen.getByRole("button", { name: "停止" }));
  });
});

describe("ResearchComposer pending regression", () => {
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

  it("submit pending中はtextareaとsendをdisabledにする", async () => {
    mocks.submit.mockReturnValue(new Promise(() => undefined));
    const user = userEvent.setup();
    renderComposer();
    const textarea = screen.getByRole("textbox", { name: "質問" });
    await user.type(textarea, "市場への影響は？");

    await user.click(screen.getByRole("button", { name: "送信" }));

    expect(mocks.submit).toHaveBeenCalledWith("市場への影響は？", "current");
    expect(textarea).toBeDisabled();
    expect(screen.getByRole("button", { name: "送信" })).toBeDisabled();
  });

  it("cancel pending中はstopをdisabledにする", async () => {
    mocks.cancel.mockReturnValue(new Promise(() => undefined));
    const user = userEvent.setup();
    renderComposer("00000000-0000-4000-a000-000000000099");

    await user.click(screen.getByRole("button", { name: "停止" }));

    expect(mocks.cancel).toHaveBeenCalledWith(
      "00000000-0000-4000-a000-000000000099",
      "current",
    );
    expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
  });
});
