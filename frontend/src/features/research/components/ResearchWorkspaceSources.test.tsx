import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type ComponentProps, type ComponentType, createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  PaginatedResearchThreadResponse,
  ResearchAssistantMessage,
  ResearchExternalUrlSource,
  ResearchInternalArticleSource,
  ResearchMessageRun,
  ResearchThreadDetail,
  ResearchUserMessage,
} from "@/types/types.gen";
import { ResearchOperationProvider } from "./ResearchOperationBoundary";
import { ResearchSubmissionProvider } from "./ResearchSubmissionBoundary";
import { ResearchWorkspace } from "./ResearchWorkspace";

vi.mock("../api/cancel-research-run", () => ({
  cancelResearchRun: vi.fn(),
}));

vi.mock("../api/delete-research-thread", () => ({
  deleteResearchThread: vi.fn(),
}));

vi.mock("../api/get-research-run", () => ({
  getResearchRun: vi.fn(),
}));

vi.mock("../api/submit-research-question", () => ({
  submitResearchQuestion: mocks.submit,
}));

const THREAD_ID = "00000000-0000-4000-a000-000000000001";
const THREAD_TWO = "00000000-0000-4000-a000-000000000002";
const RUN_ONE = "00000000-0000-4000-a000-000000000011";
const RUN_TWO = "00000000-0000-4000-a000-000000000012";
const SUBMITTED_RUN = "00000000-0000-4000-a000-000000000013";
const LONG_EXTERNAL_TITLE =
  "VeryLongExternalSourceTitleWithoutNaturalWhitespaceForOverflowVerification";
const LONG_SOURCE_NAME =
  "VeryLongSourceNameWithoutNaturalWhitespaceForOverflowVerification";
const LONG_EVIDENCE =
  "VeryLongEvidenceClaimWithoutNaturalWhitespaceForOverflowVerification";
const LONG_INTERNAL_TITLE =
  "VeryLongInternalArticleTitleWithoutNaturalWhitespaceForOverflowVerification";

const mocks = vi.hoisted(() => ({
  pathname: "/research/00000000-0000-4000-a000-000000000001",
  push: vi.fn(),
  replace: vi.fn(),
  refresh: vi.fn(),
  submit: vi.fn(),
  toast: vi.fn(),
  toastError: vi.fn(),
}));

vi.mock("@/lib/utils/toast-error", () => ({
  toastError: mocks.toastError,
}));

vi.mock("sonner", () => ({
  toast: { error: mocks.toast },
}));

vi.mock("next/navigation", () => ({
  usePathname: () => mocks.pathname,
  useSearchParams: () => new URLSearchParams(window.location.search),
  useRouter: () => ({
    push: mocks.push,
    replace: mocks.replace,
    refresh: mocks.refresh,
  }),
}));

vi.mock("next/link", () => ({
  default: ({
    onNavigate,
    ...props
  }: ComponentProps<"a"> & {
    onNavigate?: (event: { preventDefault: () => void }) => void;
  }) =>
    createElement("a", {
      ...props,
      onClick: (event) => {
        if (
          event.button === 0 &&
          !event.metaKey &&
          !event.ctrlKey &&
          !event.shiftKey &&
          !event.altKey
        ) {
          onNavigate?.({ preventDefault: () => event.preventDefault() });
        }
      },
    }),
}));

type Listener = EventListenerOrEventListenerObject;

class FakeEventSource {
  static readonly instances: FakeEventSource[] = [];
  readyState = 0;
  readonly url: string;
  private readonly listeners = new Map<string, Set<Listener>>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: Listener): void {
    const listeners = this.listeners.get(type) ?? new Set();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: Listener): void {
    this.listeners.get(type)?.delete(listener);
  }

  close(): void {
    this.readyState = 2;
  }

  emit(eventName: string, data: unknown, lastEventId: string): void {
    const event = new MessageEvent(eventName, {
      data: JSON.stringify(data),
      lastEventId,
    });
    for (const listener of this.listeners.get(eventName) ?? []) {
      if (typeof listener === "function") listener(event);
      else listener.handleEvent(event);
    }
  }
}

const externalSource: ResearchExternalUrlSource = {
  kind: "external_url",
  sourceRef: "1",
  url: "https://example.com/shared-report",
  title: LONG_EXTERNAL_TITLE,
  sourceName: LONG_SOURCE_NAME,
  publishedAt: "2026-07-10T00:00:00Z",
  evidenceClaim: LONG_EVIDENCE,
};

const internalSource: ResearchInternalArticleSource = {
  kind: "internal_article",
  sourceRef: "2",
  articleId: 42,
  title: LONG_INTERNAL_TITLE,
  publishedAt: "2026-07-09T00:00:00Z",
};

const deletedInternalSource: ResearchInternalArticleSource = {
  kind: "internal_article",
  sourceRef: "3",
  articleId: null,
  title: "Deleted internal source",
  publishedAt: null,
};

const THREADS = {
  items: [
    {
      threadId: THREAD_ID,
      title: "Sources contract",
      updatedAt: "2026-07-13T01:00:00Z",
      hasActiveRun: false,
    },
  ],
  total: 1,
  page: 1,
  perPage: 20,
  totalPages: 1,
} satisfies PaginatedResearchThreadResponse;

function run(
  runId: string,
  status: ResearchMessageRun["status"],
): ResearchMessageRun {
  return {
    runId,
    status,
    errorCode: status === "failed" ? "internal_error" : null,
    progressStage: status === "running" ? "synthesizing" : null,
  };
}

function userMessage(
  seq: number,
  runValue: ResearchMessageRun,
): ResearchUserMessage {
  return {
    role: "user",
    seq,
    content: `質問 ${seq}`,
    createdAt: `2026-07-13T00:0${seq}:00Z`,
    run: runValue,
  };
}

function assistantMessage(
  seq: number,
  answerNumber: number,
  sources: ResearchAssistantMessage["sources"],
): ResearchAssistantMessage {
  return {
    role: "assistant",
    seq,
    content: `回答本文${answerNumber} [[1]]${answerNumber === 1 ? " [[2]]" : ""}`,
    createdAt: `2026-07-13T00:0${seq}:00Z`,
    sources,
    missingAspects: [`不足事項 ${answerNumber}`],
  };
}

function completedThread(threadId = THREAD_ID): ResearchThreadDetail {
  return {
    threadId,
    title: "Sources contract",
    messages: [
      userMessage(1, run(RUN_ONE, "completed")),
      assistantMessage(2, 1, [externalSource, internalSource]),
      userMessage(3, run(RUN_TWO, "completed")),
      assistantMessage(4, 2, [{ ...externalSource }, deletedInternalSource]),
    ],
  };
}

function emptySourceThread(threadId = THREAD_ID): ResearchThreadDetail {
  return {
    threadId,
    title: "Sources contract",
    messages: [
      userMessage(1, run(RUN_ONE, "completed")),
      assistantMessage(2, 1, []),
    ],
  };
}

function activeThread(
  sources: ResearchAssistantMessage["sources"] = [externalSource],
  threadId = THREAD_ID,
): ResearchThreadDetail {
  return {
    threadId,
    title: "Sources contract",
    messages: [
      userMessage(1, run(RUN_ONE, "completed")),
      assistantMessage(2, 1, sources),
      userMessage(3, run(RUN_TWO, "running")),
    ],
  };
}

function threadWithActiveRun(
  runId: string,
  threadId = THREAD_ID,
): ResearchThreadDetail {
  return {
    threadId,
    title: "Sources contract",
    messages: [
      userMessage(1, run(RUN_ONE, "completed")),
      assistantMessage(2, 1, [externalSource, internalSource]),
      userMessage(3, run(runId, "running")),
    ],
  };
}

function threadWithFinalRun(
  runId: string,
  threadId = THREAD_ID,
): ResearchThreadDetail {
  return {
    threadId,
    title: "Sources contract",
    messages: [
      userMessage(1, run(RUN_ONE, "completed")),
      assistantMessage(2, 1, [externalSource, internalSource]),
      userMessage(3, run(runId, "completed")),
      assistantMessage(4, 2, [{ ...externalSource }, deletedInternalSource]),
    ],
  };
}

function statusThread(
  status: ResearchMessageRun["status"],
  sources: ResearchAssistantMessage["sources"] = [externalSource],
  threadId = THREAD_ID,
): ResearchThreadDetail {
  return {
    threadId,
    title: "Sources contract",
    messages: [
      userMessage(1, run(RUN_ONE, "completed")),
      assistantMessage(2, 1, sources),
      userMessage(3, run(RUN_TWO, status)),
    ],
  };
}

type WorkspaceContractProps = Omit<
  ComponentProps<typeof ResearchWorkspace>,
  "initialView"
>;

const WorkspaceUnderContract =
  ResearchWorkspace as ComponentType<WorkspaceContractProps>;

function workspaceElement(thread: ResearchThreadDetail | null) {
  return (
    <ResearchOperationProvider>
      <ResearchSubmissionProvider>
        <WorkspaceUnderContract threads={THREADS} thread={thread} limit={20} />
      </ResearchSubmissionProvider>
    </ResearchOperationProvider>
  );
}

function mediaWidth(value: string, unit: string): number {
  return Number(value) * (unit === "rem" ? 16 : 1);
}

function mediaMatches(query: string, width: number): boolean {
  const min = query.match(/min-width:\s*([\d.]+)(px|rem)/);
  const max = query.match(/max-width:\s*([\d.]+)(px|rem)/);
  if (min?.[1] && min[2] && width < mediaWidth(min[1], min[2])) return false;
  if (max?.[1] && max[2] && width > mediaWidth(max[1], max[2])) return false;
  return true;
}

function installMatchMedia(initialWidth: number): (width: number) => void {
  let width = initialWidth;
  const listeners = new Set<() => void>();
  const media = new Map<string, MediaQueryList>();

  vi.stubGlobal("matchMedia", (query: string) => {
    const existing = media.get(query);
    if (existing !== undefined) return existing;
    const list = {
      get matches() {
        return mediaMatches(query, width);
      },
      media: query,
      onchange: null,
      addEventListener: (_type: "change", listener: () => void) => {
        listeners.add(listener);
      },
      removeEventListener: (_type: "change", listener: () => void) => {
        listeners.delete(listener);
      },
      addListener: (listener: () => void) => listeners.add(listener),
      removeListener: (listener: () => void) => listeners.delete(listener),
      dispatchEvent: () => true,
    } as unknown as MediaQueryList;
    media.set(query, list);
    return list;
  });

  return (nextWidth: number) => {
    width = nextWidth;
    for (const listener of listeners) listener();
  };
}

function sourcesTrigger(): HTMLButtonElement {
  return screen.getByRole("button", { name: /ソース/ });
}

function sourcesAside(): HTMLElement {
  return screen.getByRole("complementary", { name: "ソース" });
}

function sourcesDialog(): HTMLElement {
  return screen.getByRole("dialog", { name: "ソース" });
}

function composerSpacer(): HTMLElement | null {
  let candidate = document.querySelector<HTMLElement>("#research-question");
  if (candidate === null) throw new Error("composer textarea is missing");
  const threadPane = candidate.closest<HTMLElement>("section");
  while (candidate.parentElement !== null && candidate !== threadPane) {
    const spacer = Array.from(candidate.parentElement.children).find(
      (sibling) =>
        sibling !== candidate &&
        sibling instanceof HTMLElement &&
        sibling.tagName === "DIV" &&
        sibling.getAttribute("aria-hidden") === "true" &&
        sibling.childElementCount === 0,
    );
    if (spacer instanceof HTMLElement) return spacer;
    candidate = candidate.parentElement;
  }
  return null;
}

function expectSourcesClosed(): void {
  const trigger = sourcesTrigger();
  expect(trigger).toHaveAttribute("aria-expanded", "false");
  expect(trigger).not.toHaveAttribute("aria-controls");
  expect(
    screen.queryByRole("complementary", { name: "ソース", hidden: true }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("dialog", { name: "ソース", hidden: true }),
  ).toBeNull();
  expect(composerSpacer()).toBeNull();
}

function scrollOwner(root: HTMLElement): HTMLElement {
  const candidate = [root, ...root.querySelectorAll<HTMLElement>("*")].find(
    (element) => element.classList.contains("overflow-y-auto"),
  );
  if (candidate === undefined) throw new Error("scroll owner is missing");
  return candidate;
}

function answerScroller(): HTMLElement {
  const slot = screen.getAllByTestId("research-answer-slot")[0];
  if (slot === undefined) throw new Error("answer slot is missing");
  let candidate: HTMLElement | null = slot;
  while (
    candidate !== null &&
    !candidate.classList.contains("overflow-y-auto")
  ) {
    candidate = candidate.parentElement;
  }
  if (candidate === null) throw new Error("answer scroller is missing");
  return candidate;
}

function latestAnswerSlot(): HTMLElement {
  const slot = screen.getAllByTestId("research-answer-slot").at(-1);
  if (slot === undefined) throw new Error("latest answer slot is missing");
  return slot;
}

type Deferred<T> = {
  promise: Promise<T>;
  resolve: (value: T) => void;
};

function createDeferred<T>(): Deferred<T> {
  let resolvePromise: ((value: T) => void) | undefined;
  const promise = new Promise<T>((resolve) => {
    resolvePromise = resolve;
  });
  return {
    promise,
    resolve: (value) => resolvePromise?.(value),
  };
}

function expectOverflowSafe(element: HTMLElement): void {
  let candidate: HTMLElement | null = element;
  while (candidate !== null) {
    const safe =
      candidate.classList.contains("break-words") ||
      candidate.className.includes("overflow-wrap:anywhere") ||
      Array.from(candidate.classList).some((name) =>
        /^(?:line-clamp|truncate)/.test(name),
      );
    if (safe) return;
    candidate = candidate.parentElement;
  }
  throw new Error("overflow-safe presentation is missing");
}

function onlyLiveAnnouncer(container: HTMLElement): HTMLElement {
  const owners = Array.from(
    container.querySelectorAll<HTMLElement>('[role="status"], [aria-live]'),
  );
  expect(owners).toHaveLength(1);
  const announcer = owners[0];
  if (announcer === undefined) throw new Error("live announcer is missing");
  return announcer;
}

beforeEach(() => {
  mocks.pathname = `/research/${THREAD_ID}`;
  window.history.replaceState(null, "", `/research/${THREAD_ID}`);
  mocks.push.mockReset();
  mocks.replace.mockReset();
  mocks.refresh.mockReset();
  mocks.submit.mockReset();
  mocks.toast.mockReset();
  mocks.toastError.mockReset();
  FakeEventSource.instances.length = 0;
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal(
    "fetch",
    vi.fn(() => new Promise<Response>(() => undefined)),
  );
  vi.stubGlobal(
    "requestAnimationFrame",
    vi.fn(() => 1),
  );
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Research workspace sources disclosure", () => {
  it.each([
    ["wide", 1280],
    ["compact", 1279],
  ] as const)("sourceありの%s初期表示をclosedに保つ", (_label, width) => {
    installMatchMedia(width);
    render(workspaceElement(completedThread()));

    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
    expect(screen.queryByRole("tabpanel")).not.toBeInTheDocument();
    expect(sourcesTrigger()).toBeEnabled();
    expect(sourcesTrigger()).toHaveTextContent("4");
    expectSourcesClosed();
  });

  it("wideではtrigger操作だけでinline surfaceとcomposer spacerを同時にopen/closeする", async () => {
    installMatchMedia(1280);
    const user = userEvent.setup();
    render(workspaceElement(completedThread()));
    const trigger = sourcesTrigger();
    expectSourcesClosed();

    await user.click(trigger);
    const aside = sourcesAside();
    const stableId = aside.id;
    expect(stableId).not.toBe("");
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(trigger).toHaveAttribute("aria-controls", stableId);
    expect(screen.queryByRole("dialog", { name: "ソース" })).toBeNull();
    expect(composerSpacer()).not.toBeNull();
    expect(scrollOwner(aside)).not.toBe(answerScroller());

    await user.click(trigger);
    expectSourcesClosed();

    await user.click(trigger);
    expect(sourcesAside().id).toBe(stableId);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(trigger).toHaveAttribute("aria-controls", stableId);
  });

  it("compactではtrigger操作だけでmodalを開きEscape/close後にcontrolsを消してfocusを戻す", async () => {
    installMatchMedia(1279);
    const user = userEvent.setup();
    render(workspaceElement(completedThread()));
    const trigger = sourcesTrigger();
    expectSourcesClosed();

    await user.click(trigger);
    const dialog = sourcesDialog();
    const stableId = dialog.id;
    expect(stableId).not.toBe("");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveClass("right-0", "border-l");
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(trigger).toHaveAttribute("aria-controls", stableId);
    expect(
      screen.queryByRole("complementary", { name: "ソース" }),
    ).not.toBeInTheDocument();
    expect(composerSpacer()).toBeNull();

    await user.keyboard("{Escape}");
    await waitFor(expectSourcesClosed);
    expect(trigger).toHaveFocus();

    await user.click(trigger);
    const reopened = sourcesDialog();
    expect(reopened.id).toBe(stableId);
    await user.click(
      within(reopened).getByRole("button", { name: "ソースを閉じる" }),
    );
    await waitFor(expectSourcesClosed);
    expect(trigger).toHaveFocus();
  });

  it("open中の1279pxと1280px crossingをclosedへ収束させ反対surfaceを自動openしない", async () => {
    const setWidth = installMatchMedia(1279);
    const user = userEvent.setup();
    render(workspaceElement(completedThread()));
    const trigger = sourcesTrigger();

    await user.click(trigger);
    const sheetId = sourcesDialog().id;
    expect(trigger).toHaveAttribute("aria-controls", sheetId);
    act(() => setWidth(1280));
    await waitFor(expectSourcesClosed);

    await user.click(trigger);
    const inlineId = sourcesAside().id;
    expect(inlineId).not.toBe(sheetId);
    expect(trigger).toHaveAttribute("aria-controls", inlineId);
    expect(screen.queryByRole("dialog", { name: "ソース" })).toBeNull();
    act(() => setWidth(1279));
    await waitFor(expectSourcesClosed);

    await user.click(trigger);
    expect(sourcesDialog().id).toBe(sheetId);
    expect(trigger).toHaveAttribute("aria-controls", sheetId);
    expect(
      screen.queryByRole("complementary", { name: "ソース" }),
    ).not.toBeInTheDocument();
    act(() => setWidth(1280));
    await waitFor(expectSourcesClosed);
  });

  it.each([
    ["wide", 1280],
    ["compact", 1279],
  ] as const)("%sの0 sourcesからpositiveへの更新はbuttonだけをenableにする", async (_label, width) => {
    installMatchMedia(width);
    const user = userEvent.setup();
    const view = render(workspaceElement(emptySourceThread()));
    const trigger = sourcesTrigger();

    expect(trigger).toBeDisabled();
    expect(trigger).toHaveTextContent("0");
    expectSourcesClosed();
    await user.click(trigger);
    expectSourcesClosed();

    view.rerender(workspaceElement(completedThread()));
    expect(sourcesTrigger()).toBe(trigger);
    expect(trigger).toBeEnabled();
    expect(trigger).toHaveTextContent("4");
    expectSourcesClosed();
  });

  it("DB assistant sourcesを回答単位で重複保持し全kindと長文を安全に表示する", async () => {
    installMatchMedia(1280);
    const user = userEvent.setup();
    render(workspaceElement(completedThread()));
    await user.click(sourcesTrigger());
    const aside = sourcesAside();
    const firstGroup = within(aside).getByText("回答 1");
    const secondGroup = within(aside).getByText("回答 2");
    expect(
      firstGroup.compareDocumentPosition(secondGroup) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    const duplicated = within(aside).getAllByRole("link", {
      name: LONG_EXTERNAL_TITLE,
    });
    expect(duplicated).toHaveLength(2);
    for (const link of duplicated) {
      expect(link).toHaveAttribute("href", externalSource.url);
      expect(link).toHaveAttribute("target", "_blank");
      expect(link).toHaveAttribute("rel", "noreferrer");
      expectOverflowSafe(link);
    }
    expect(
      within(aside).getByRole("link", { name: LONG_INTERNAL_TITLE }),
    ).toHaveAttribute("href", "/news/42");
    expect(
      within(aside).queryByRole("link", { name: "Deleted internal source" }),
    ).toBeNull();
    expect(within(aside).getByText("Deleted internal source")).toBeVisible();
    for (const element of within(aside).getAllByText(LONG_SOURCE_NAME)) {
      expectOverflowSafe(element);
    }
    for (const element of within(aside).getAllByText(LONG_EVIDENCE)) {
      expectOverflowSafe(element);
    }
  });

  it("same-threadのsource/status更新で選択状態と主要DOM identityを維持しlive draftをsourcesへ混ぜない", async () => {
    installMatchMedia(1280);
    const user = userEvent.setup();
    const view = render(workspaceElement(activeThread()));
    const trigger = sourcesTrigger();
    const textarea = screen.getByRole("textbox", { name: "質問" });
    fireEvent.change(textarea, { target: { value: "保持する入力" } });
    const scroller = answerScroller();
    scroller.scrollTop = 123;
    const stableAnswerSlot = screen.getAllByTestId("research-answer-slot")[0];
    const eventSource = FakeEventSource.instances[0];
    if (stableAnswerSlot === undefined)
      throw new Error("answer slot is missing");
    if (eventSource === undefined) throw new Error("EventSource is missing");
    const announcer = onlyLiveAnnouncer(view.container);
    expectSourcesClosed();

    await user.click(trigger);
    const aside = sourcesAside();
    const sourceScroll = scrollOwner(aside);
    sourceScroll.scrollTop = 77;
    trigger.focus();

    view.rerender(
      workspaceElement(activeThread([externalSource, internalSource])),
    );
    expect(sourcesTrigger()).toBe(trigger);
    expect(sourcesAside()).toBe(aside);
    expect(scrollOwner(aside)).toBe(sourceScroll);
    expect(sourceScroll.scrollTop).toBe(77);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(trigger).toHaveAttribute("aria-controls", aside.id);
    expect(screen.getByRole("textbox", { name: "質問" })).toBe(textarea);
    expect(textarea).toHaveValue("保持する入力");
    expect(answerScroller()).toBe(scroller);
    expect(scroller.scrollTop).toBe(123);
    expect(screen.getAllByTestId("research-answer-slot")[0]).toBe(
      stableAnswerSlot,
    );
    expect(FakeEventSource.instances).toEqual([eventSource]);
    expect(onlyLiveAnnouncer(view.container)).toBe(announcer);
    expect(trigger).toHaveFocus();

    act(() => {
      eventSource.emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "sourceへ混ぜないdraft" },
        "1-0",
      );
    });
    expect(within(aside).queryByText("sourceへ混ぜないdraft")).toBeNull();
    expect(within(aside).getAllByText(/回答 \d+/)).toHaveLength(1);
    expect(FakeEventSource.instances).toEqual([eventSource]);

    for (const status of [
      "queued",
      "running",
      "completed",
      "failed",
    ] as const) {
      view.rerender(
        workspaceElement(
          statusThread(status, [externalSource, internalSource]),
        ),
      );
      expect(sourcesAside()).toBe(aside);
      expect(scrollOwner(aside)).toBe(sourceScroll);
      expect(sourceScroll.scrollTop).toBe(77);
      expect(screen.getByRole("textbox", { name: "質問" })).toBe(textarea);
      expect(answerScroller()).toBe(scroller);
      expect(screen.getAllByTestId("research-answer-slot")[0]).toBe(
        stableAnswerSlot,
      );
      expect(onlyLiveAnnouncer(view.container)).toBe(announcer);
      expect(trigger).toHaveFocus();
    }

    await user.click(trigger);
    expectSourcesClosed();
    view.rerender(workspaceElement(statusThread("running", [externalSource])));
    expectSourcesClosed();
    expect(screen.getByRole("textbox", { name: "質問" })).toBe(textarea);
    expect(answerScroller()).toBe(scroller);
    expect(onlyLiveAnnouncer(view.container)).toBe(announcer);
    expect(trigger).toHaveFocus();
  });

  it("threadId A→BとB→Aのrerenderを同期的にclosedで開始する", async () => {
    installMatchMedia(1280);
    const user = userEvent.setup();
    const view = render(workspaceElement(completedThread(THREAD_ID)));

    await user.click(sourcesTrigger());
    expect(sourcesAside()).toBeInTheDocument();
    view.rerender(workspaceElement(completedThread(THREAD_TWO)));
    expectSourcesClosed();

    await user.click(sourcesTrigger());
    expect(sourcesAside()).toBeInTheDocument();
    view.rerender(workspaceElement(completedThread(THREAD_ID)));
    expectSourcesClosed();
  });

  it("threadなしではtabsもsources trigger/panel/sheetも描画しない", () => {
    installMatchMedia(390);
    render(workspaceElement(null));

    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.queryByRole("tab")).toBeNull();
    expect(screen.queryByRole("tabpanel")).toBeNull();
    expect(screen.queryByRole("button", { name: /ソース/ })).toBeNull();
    expect(screen.queryByRole("complementary", { name: "ソース" })).toBeNull();
    expect(screen.queryByRole("dialog", { name: "ソース" })).toBeNull();
  });
});

describe("Research workspace submission feedback", () => {
  async function beginSubmission(
    user: ReturnType<typeof userEvent.setup>,
    question: string,
  ): Promise<{ form: HTMLFormElement; textarea: HTMLTextAreaElement }> {
    const textarea = screen.getByRole<HTMLTextAreaElement>("textbox", {
      name: "質問",
    });
    const form = textarea.closest("form");
    if (!(form instanceof HTMLFormElement)) {
      throw new Error("composer form is missing");
    }

    await user.type(textarea, question);
    await user.click(screen.getByRole("button", { name: "送信" }));
    return { form, textarea };
  }

  function expectVisibleSubmissionStatus(main: HTMLElement): void {
    const statuses = within(main).getAllByRole("status", {
      name: "質問を送信しています…",
    });
    expect(statuses).toHaveLength(1);
    expect(statuses[0]).toBeVisible();
    expect(statuses[0]).not.toHaveClass("sr-only");
  }

  it("unresolved submit中も既存answerを保ったmain内に進行statusを一つだけ表示する", async () => {
    installMatchMedia(1280);
    mocks.submit.mockReturnValue(new Promise(() => undefined));
    const user = userEvent.setup();
    render(workspaceElement(completedThread()));
    const main = screen.getByRole("main");
    expect(within(main).getByText(/回答本文1/)).toBeInTheDocument();

    const { form } = await beginSubmission(user, "市場への影響は？");

    expect(mocks.submit).toHaveBeenCalledWith("市場への影響は？", THREAD_ID);
    expect(form).toHaveAttribute("aria-busy", "true");
    expect(
      within(form).getByRole("button", { name: "送信中…" }),
    ).toBeDisabled();
    expectVisibleSubmissionStatus(main);
    expect(within(main).getByText(/回答本文1/)).toBeInTheDocument();
    expect(screen.queryByTestId("page-navigation-overlay")).toBeNull();
  });

  it("empty workspaceでもmain内にvisibleなsubmit statusを表示する", async () => {
    installMatchMedia(1280);
    mocks.submit.mockReturnValue(new Promise(() => undefined));
    const user = userEvent.setup();
    render(workspaceElement(null));
    const main = screen.getByRole("main");

    const { form } = await beginSubmission(user, "新しい質問");

    expect(form).toHaveAttribute("aria-busy", "true");
    expectVisibleSubmissionStatus(main);
    expect(screen.queryByTestId("page-navigation-overlay")).toBeNull();
  });

  it("submit失敗後はstatusを消して入力とidle controlsを戻す", async () => {
    installMatchMedia(1280);
    const error = new Error("submit failed");
    mocks.submit.mockRejectedValue(error);
    const user = userEvent.setup();
    render(workspaceElement(completedThread()));
    const main = screen.getByRole("main");

    const { form, textarea } = await beginSubmission(user, "保持する質問");

    await waitFor(() =>
      expect(mocks.toastError).toHaveBeenCalledWith(
        error,
        "質問を送信できませんでした",
      ),
    );
    await waitFor(() =>
      expect(within(form).getByRole("button", { name: "送信" })).toBeEnabled(),
    );
    expect(form).not.toHaveAttribute("aria-busy", "true");
    expect(
      within(main).queryByRole("status", { name: "質問を送信しています…" }),
    ).toBeNull();
    expect(textarea).toHaveValue("保持する質問");
    expect(screen.queryByTestId("page-navigation-overlay")).toBeNull();
  });
});

describe("Research workspace model-commit continuity", () => {
  async function beginSubmission(
    user: ReturnType<typeof userEvent.setup>,
    question: string,
  ): Promise<HTMLFormElement> {
    const textarea = screen.getByRole<HTMLTextAreaElement>("textbox", {
      name: "質問",
    });
    const form = textarea.closest("form");
    if (!(form instanceof HTMLFormElement)) {
      throw new Error("composer form is missing");
    }

    await user.type(textarea, question);
    await user.click(screen.getByRole("button", { name: "送信" }));
    return form;
  }

  function expectVisibleSubmissionStatus(main: HTMLElement): void {
    const statuses = within(main).getAllByRole("status", {
      name: "質問を送信しています…",
    });
    expect(statuses).toHaveLength(1);
    expect(statuses[0]).toBeVisible();
    expect(statuses[0]).not.toHaveClass("sr-only");
  }

  it("accepted submitは同threadの対象runがmodelへcommitするまでbusy/statusを保つ", async () => {
    installMatchMedia(1280);
    const accepted = createDeferred<{
      kind: "accepted";
      run: { threadId: string; runId: string };
    }>();
    mocks.submit.mockReturnValue(accepted.promise);
    const user = userEvent.setup();
    const view = render(workspaceElement(completedThread()));
    const main = screen.getByRole("main");

    const form = await beginSubmission(user, "commitを待つ質問");
    await act(async () => {
      accepted.resolve({
        kind: "accepted",
        run: { threadId: THREAD_ID, runId: SUBMITTED_RUN },
      });
      await Promise.resolve();
    });

    expect(form).toHaveAttribute("aria-busy", "true");
    expect(
      within(form).getByRole("button", { name: "送信中…" }),
    ).toBeDisabled();
    expectVisibleSubmissionStatus(main);

    view.rerender(workspaceElement(threadWithActiveRun(RUN_TWO)));

    expect(form).toHaveAttribute("aria-busy", "true");
    expectVisibleSubmissionStatus(main);

    view.rerender(workspaceElement(threadWithActiveRun(SUBMITTED_RUN)));

    await waitFor(() => expect(form).toHaveAttribute("aria-busy", "false"));
    expect(
      within(main).queryByRole("status", { name: "質問を送信しています…" }),
    ).toBeNull();
  });

  it("new thread accepted後もclient navigationと対象model commitまでempty workspaceのpendingを保つ", async () => {
    installMatchMedia(1280);
    mocks.submit.mockResolvedValue({
      kind: "accepted",
      run: { threadId: THREAD_TWO, runId: SUBMITTED_RUN },
    });
    const user = userEvent.setup();
    const view = render(workspaceElement(null));
    const main = screen.getByRole("main");
    const frame = main;
    const form = await beginSubmission(user, "新規threadへの質問");

    await waitFor(() => expect(mocks.submit).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(mocks.replace).toHaveBeenCalledWith(`/research/${THREAD_TWO}`),
    );
    expect(mocks.replace).toHaveBeenCalledTimes(1);
    expect(mocks.refresh).not.toHaveBeenCalled();
    expect(mocks.toastError).not.toHaveBeenCalled();
    expect(form).toHaveAttribute("aria-busy", "true");
    expectVisibleSubmissionStatus(main);
    expect(screen.getByRole("main")).toBe(frame);
    expect(screen.queryByTestId("page-navigation-overlay")).toBeNull();

    mocks.pathname = `/research/${THREAD_TWO}`;
    window.history.replaceState(null, "", mocks.pathname);
    view.rerender(
      workspaceElement(threadWithActiveRun(SUBMITTED_RUN, THREAD_TWO)),
    );

    await waitFor(() =>
      expect(screen.getByRole("main")).toHaveAttribute("aria-busy", "false"),
    );
    expect(
      within(main).queryByRole("status", { name: "質問を送信しています…" }),
    ).toBeNull();
  });

  it("emptyからactive thread modelへcommitしてもworkspace frameとcomposerを置換しない", () => {
    installMatchMedia(1280);
    const view = render(workspaceElement(null));
    const frame = screen.getByRole("main");
    const composer = screen
      .getByRole("textbox", { name: "質問" })
      .closest("form");
    if (!(composer instanceof HTMLFormElement)) {
      throw new Error("composer form is missing");
    }

    view.rerender(workspaceElement(threadWithActiveRun(SUBMITTED_RUN)));

    expect(screen.getByRole("main")).toBe(frame);
    expect(screen.getByRole("textbox", { name: "質問" }).closest("form")).toBe(
      composer,
    );
    expect(
      screen.queryByRole("status", { name: "Researchを読み込み中…" }),
    ).toBeNull();
    expect(screen.queryByTestId("page-navigation-overlay")).toBeNull();
  });

  it("outer workspace model commitでもtarget slot/scrollerを保ちdraftからfinalへ排他的に置換する", () => {
    installMatchMedia(1280);
    const view = render(workspaceElement(threadWithActiveRun(SUBMITTED_RUN)));
    const source = FakeEventSource.instances.at(-1);
    if (source === undefined) throw new Error("EventSource is missing");
    const scroller = answerScroller();
    const slot = latestAnswerSlot();

    act(() => {
      source.emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "外側commit前の下書き" },
        "1-0",
      );
    });
    expect(slot).toContainElement(screen.getByText("外側commit前の下書き"));
    expect(within(slot).queryByText("回答本文2")).toBeNull();
    expect(slot.textContent?.trim().length).toBeGreaterThan(0);

    act(() => {
      source.emit("terminal", { attemptEpoch: 1, status: "completed" }, "2-0");
    });
    expect(latestAnswerSlot()).toBe(slot);
    expect(slot).toHaveTextContent("外側commit前の下書き");
    expect(slot).toHaveTextContent("回答を確定しています…");
    expect(within(slot).queryByText("回答本文2")).toBeNull();

    view.rerender(workspaceElement(threadWithFinalRun(SUBMITTED_RUN)));

    expect(answerScroller()).toBe(scroller);
    expect(latestAnswerSlot()).toBe(slot);
    expect(within(slot).queryByText("外側commit前の下書き")).toBeNull();
    expect(within(slot).getByText(/回答本文2/)).toBeInTheDocument();
    expect(slot).not.toHaveTextContent("回答を確定しています…");
    expect(slot.textContent?.trim().length).toBeGreaterThan(0);
  });
});
