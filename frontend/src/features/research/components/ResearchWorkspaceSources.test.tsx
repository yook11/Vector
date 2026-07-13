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
  submitResearchQuestion: vi.fn(),
}));

const THREAD_ID = "00000000-0000-4000-a000-000000000001";
const RUN_ONE = "00000000-0000-4000-a000-000000000011";
const RUN_TWO = "00000000-0000-4000-a000-000000000012";
const LONG_EXTERNAL_TITLE =
  "VeryLongExternalSourceTitleWithoutNaturalWhitespaceForOverflowVerification";
const LONG_SOURCE_NAME =
  "VeryLongSourceNameWithoutNaturalWhitespaceForOverflowVerification";
const LONG_EVIDENCE =
  "VeryLongEvidenceClaimWithoutNaturalWhitespaceForOverflowVerification";
const LONG_INTERNAL_TITLE =
  "VeryLongInternalArticleTitleWithoutNaturalWhitespaceForOverflowVerification";

const mocks = vi.hoisted(() => ({
  push: vi.fn(),
  refresh: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => `/research/${THREAD_ID}`,
  useSearchParams: () => new URLSearchParams(window.location.search),
  useRouter: () => ({ push: mocks.push, refresh: mocks.refresh }),
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
    errorCode: null,
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

function completedThread(): ResearchThreadDetail {
  return {
    threadId: THREAD_ID,
    title: "Sources contract",
    messages: [
      userMessage(1, run(RUN_ONE, "completed")),
      assistantMessage(2, 1, [externalSource, internalSource]),
      userMessage(3, run(RUN_TWO, "completed")),
      assistantMessage(4, 2, [{ ...externalSource }, deletedInternalSource]),
    ],
  };
}

function emptySourceThread(): ResearchThreadDetail {
  return {
    threadId: THREAD_ID,
    title: "Sources contract",
    messages: [
      userMessage(1, run(RUN_ONE, "completed")),
      assistantMessage(2, 1, []),
    ],
  };
}

function activeThread(): ResearchThreadDetail {
  return {
    threadId: THREAD_ID,
    title: "Sources contract",
    messages: [
      userMessage(1, run(RUN_ONE, "completed")),
      assistantMessage(2, 1, [externalSource]),
      userMessage(3, run(RUN_TWO, "running")),
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
    <WorkspaceUnderContract threads={THREADS} thread={thread} limit={20} />
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
  window.history.replaceState(null, "", `/research/${THREAD_ID}`);
  mocks.push.mockReset();
  mocks.refresh.mockReset();
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

describe("S3R Research workspace sources", () => {
  it("threadではtabsなしでwide inline sourcesを初期表示し、独立scroll ownerのままclose/reopenする", async () => {
    installMatchMedia(1280);
    const user = userEvent.setup();
    render(workspaceElement(completedThread()));

    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
    expect(screen.queryByRole("tabpanel")).not.toBeInTheDocument();
    const trigger = sourcesTrigger();
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(trigger).toHaveTextContent("4");
    const aside = sourcesAside();
    expect(trigger).toHaveAttribute("aria-controls", aside.id);
    expect(scrollOwner(aside)).not.toBe(answerScroller());

    await user.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(
      screen.queryByRole("complementary", { name: "ソース" }),
    ).not.toBeInTheDocument();

    await user.click(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(sourcesAside()).toBeInTheDocument();
  });

  it("compactではinlineと同時表示せず右modal sheetをEscape/closeで閉じてtriggerへfocusを返す", async () => {
    const setWidth = installMatchMedia(1279);
    const user = userEvent.setup();
    render(workspaceElement(completedThread()));

    const trigger = sourcesTrigger();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(
      screen.queryByRole("complementary", { name: "ソース" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "ソース" })).toBeNull();

    await user.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "ソース" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveClass("right-0", "border-l");
    expect(
      screen.queryByRole("complementary", { name: "ソース" }),
    ).not.toBeInTheDocument();

    act(() => setWidth(1280));
    expect(screen.queryByRole("dialog", { name: "ソース" })).toBeNull();
    expect(sourcesAside()).toBeInTheDocument();

    act(() => setWidth(1279));
    expect(
      screen.queryByRole("complementary", { name: "ソース" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "ソース" })).toBeNull();

    await user.click(trigger);
    await user.keyboard("{Escape}");
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "ソース" })).toBeNull(),
    );
    expect(trigger).toHaveFocus();

    await user.click(trigger);
    const reopened = screen.getByRole("dialog", { name: "ソース" });
    await user.click(
      within(reopened).getByRole("button", {
        name: /ソースを閉じる|閉じる|Close/,
      }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "ソース" })).toBeNull(),
    );
    expect(trigger).toHaveFocus();
  });

  it("API sourcesを回答順に重複保持し全kindとlong fieldを安全に表示し、0件ではdisabled empty stateにする", () => {
    installMatchMedia(1280);
    const view = render(workspaceElement(completedThread()));
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

    view.rerender(workspaceElement(emptySourceThread()));
    expect(sourcesTrigger()).toBeDisabled();
    expect(sourcesTrigger()).toHaveTextContent("0");
    const emptyAside = sourcesAside();
    expect(emptyAside).toHaveTextContent("表示できるソースはありません");
    expect(within(emptyAside).queryByRole("link")).toBeNull();
  });

  it("sources open/closeとlive deltaでtextarea・answer scroll・slot・EventSource・single announcerを維持する", async () => {
    installMatchMedia(1280);
    const user = userEvent.setup();
    const view = render(workspaceElement(activeThread()));
    const trigger = sourcesTrigger();
    const textarea = screen.getByRole("textbox", { name: "質問" });
    fireEvent.change(textarea, { target: { value: "保持する入力" } });
    const scroller = answerScroller();
    scroller.scrollTop = 123;
    const answerSlot = screen.getAllByTestId("research-answer-slot").at(-1);
    const source = FakeEventSource.instances[0];
    if (answerSlot === undefined) throw new Error("answer slot is missing");
    if (source === undefined) throw new Error("EventSource is missing");
    const announcer = onlyLiveAnnouncer(view.container);

    await user.click(trigger);
    await user.click(trigger);

    expect(screen.getByRole("textbox", { name: "質問" })).toBe(textarea);
    expect(textarea).toHaveValue("保持する入力");
    expect(answerScroller()).toBe(scroller);
    expect(scroller.scrollTop).toBe(123);
    expect(screen.getAllByTestId("research-answer-slot").at(-1)).toBe(
      answerSlot,
    );
    expect(FakeEventSource.instances).toEqual([source]);
    expect(onlyLiveAnnouncer(view.container)).toBe(announcer);

    act(() => {
      source.emit(
        "answer.delta",
        { attemptEpoch: 1, generation: 1, text: "sourceへ混ぜないdraft" },
        "1-0",
      );
    });

    expect(
      within(sourcesAside()).queryByText("sourceへ混ぜないdraft"),
    ).toBeNull();
    expect(within(sourcesAside()).getAllByText(/回答 \d+/)).toHaveLength(1);
    expect(FakeEventSource.instances).toEqual([source]);
    expect(screen.getByRole("textbox", { name: "質問" })).toBe(textarea);
    expect(onlyLiveAnnouncer(view.container)).toBe(announcer);
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
