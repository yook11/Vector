import type { Page, Request, Route } from "@playwright/test";
import type { ResearchRunResponse } from "@/types/types.gen";
import type { ResearchContinuityFixture } from "./research";

const LONG_DRAFT = Array.from(
  { length: 18 },
  (_, index) =>
    `E2E continuity live draft marker ${index + 1}: terminal convergenceの間も回答領域を維持します。`,
).join("\n");

export interface ResearchContinuityRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface ResearchContinuityPaintSample {
  timestamp: number;
  mutationObserved: boolean;
  documentToken: string;
  persistedStatus: string | null;
  draftCount: number;
  failureCount: number;
  protectedLoadingCount: number;
  announcerCount: number;
  sourceSurfaceCount: number;
  sourcesExpanded: string | null;
  sourcesControls: string | null;
  sourceScrollTop: number | null;
  focusedHref: string | null;
  sameFocus: boolean;
  sameTurn: boolean;
  sameFailureRail: boolean | null;
  sameThreadPanel: boolean;
  sameComposer: boolean;
  sameAnswerScroller: boolean;
  sameSourceScroller: boolean | null;
  turnRect: ResearchContinuityRect;
  threadPanelRect: ResearchContinuityRect;
  composerRect: ResearchContinuityRect;
  answerScrollTop: number;
  answerScrollHeight: number;
  answerClientHeight: number;
}

export interface ResearchContinuityHarnessStats {
  targetPollResponses: number;
  targetPollStatuses: readonly string[];
  terminalRscRequests: number;
  terminalMainFrameNavigations: number;
  eventSourcesCreated: number;
  eventSourcesClosed: number;
  draftEventsSent: number;
  terminalEventsSent: number;
  documentToken: string;
}

interface BrowserEventSourceStats {
  eventSourcesCreated: number;
  eventSourcesClosed: number;
  draftEventsSent: number;
  terminalEventsSent: number;
  documentToken: string;
}

interface BrowserContinuityBridge {
  documentToken: string;
  emitDraft: () => void;
  emitFailure: () => void;
  getEventSourceStats: () => BrowserEventSourceStats;
  sampler?: {
    done: boolean;
    samples: ResearchContinuityPaintSample[];
  };
}

type ContinuityWindow = Window & {
  __researchContinuity?: BrowserContinuityBridge;
};

export interface ResearchContinuityBrowserHarness {
  emitDraft: () => Promise<void>;
  startSampler: () => Promise<ResearchContinuityPaintSample>;
  armTerminalRefreshGate: () => void;
  emitFailedTerminal: () => Promise<void>;
  waitForTerminalRefresh: () => Promise<void>;
  releaseTerminalRefresh: () => void;
  waitForPersistedSample: () => Promise<void>;
  samples: () => Promise<ResearchContinuityPaintSample[]>;
  stats: () => Promise<ResearchContinuityHarnessStats>;
  cleanup: () => Promise<void>;
}

function isTargetGet(route: Route, pathname: string): boolean {
  const request = route.request();
  const url = new URL(request.url());
  return (
    request.method() === "GET" && url.pathname === pathname && url.search === ""
  );
}

function isPrefetch(headers: Record<string, string>): boolean {
  return (
    headers["next-router-prefetch"] === "1" ||
    headers.purpose === "prefetch" ||
    headers["sec-purpose"]?.includes("prefetch") === true
  );
}

export async function installResearchContinuityBrowserHarness(
  page: Page,
  fixture: ResearchContinuityFixture,
): Promise<ResearchContinuityBrowserHarness> {
  const eventPath = `/api/research/runs/${fixture.activeRunId}/events`;
  const pollPath = `/api/research/runs/${fixture.activeRunId}`;
  const threadPath = `/research/${fixture.threadId}`;

  await page.addInitScript(
    ({ targetEventPath, draftText }) => {
      const NativeEventSource = window.EventSource;
      let currentSource: ControlledEventSource | null = null;
      let eventSourcesCreated = 0;
      let eventSourcesClosed = 0;
      let draftEventsSent = 0;
      let terminalEventsSent = 0;
      let measurementStartCreated: number | null = null;
      let measurementStartClosed: number | null = null;

      class ControlledEventSource extends EventTarget {
        readonly CONNECTING = NativeEventSource.CONNECTING;
        readonly OPEN = NativeEventSource.OPEN;
        readonly CLOSED = NativeEventSource.CLOSED;
        readonly url: string;
        readonly withCredentials = false;
        readyState: number = NativeEventSource.CONNECTING;
        onerror: ((this: EventSource, ev: Event) => unknown) | null = null;
        onmessage:
          | ((this: EventSource, ev: MessageEvent<unknown>) => unknown)
          | null = null;
        onopen: ((this: EventSource, ev: Event) => unknown) | null = null;

        constructor(url: string) {
          super();
          this.url = url;
        }

        close(): void {
          if (this.readyState === NativeEventSource.CLOSED) return;
          this.readyState = NativeEventSource.CLOSED;
          eventSourcesClosed += 1;
        }

        open(): void {
          if (this.readyState !== NativeEventSource.CONNECTING) return;
          this.readyState = NativeEventSource.OPEN;
          this.dispatchEvent(new Event("open"));
        }

        emit(type: string, data: object, lastEventId: string): void {
          this.dispatchEvent(
            new MessageEvent(type, {
              data: JSON.stringify(data),
              lastEventId,
            }),
          );
        }
      }

      const EventSourceProxy = new Proxy(NativeEventSource, {
        construct(target, argumentsList) {
          const rawUrl = String(argumentsList[0]);
          const url = new URL(rawUrl, window.location.href);
          if (url.pathname !== targetEventPath || url.search !== "") {
            return Reflect.construct(target, argumentsList, target);
          }
          const source = new ControlledEventSource(url.href);
          currentSource = source;
          eventSourcesCreated += 1;
          return source;
        },
      });
      Object.defineProperty(window, "EventSource", {
        configurable: true,
        writable: true,
        value: EventSourceProxy,
      });

      const continuityWindow = window as ContinuityWindow;
      const documentToken = crypto.randomUUID();
      continuityWindow.__researchContinuity = {
        documentToken,
        emitDraft: () => {
          if (currentSource === null) {
            throw new Error("Target research EventSource is not connected");
          }
          measurementStartCreated = Math.max(0, eventSourcesCreated - 1);
          measurementStartClosed = eventSourcesClosed;
          currentSource.open();
          currentSource.emit("attempt.started", { attemptEpoch: 1 }, "1-0");
          currentSource.emit(
            "stage",
            { attemptEpoch: 1, stage: "synthesizing" },
            "2-0",
          );
          currentSource.emit(
            "answer.delta",
            { attemptEpoch: 1, generation: 1, text: draftText },
            "3-0",
          );
          draftEventsSent += 1;
        },
        emitFailure: () => {
          if (currentSource === null) {
            throw new Error("Target research EventSource is not connected");
          }
          terminalEventsSent += 1;
          currentSource.emit(
            "terminal",
            {
              attemptEpoch: 1,
              status: "failed",
              errorCode: "internal_error",
            },
            "4-0",
          );
        },
        getEventSourceStats: () => ({
          eventSourcesCreated:
            eventSourcesCreated - (measurementStartCreated ?? 0),
          eventSourcesClosed:
            eventSourcesClosed - (measurementStartClosed ?? 0),
          draftEventsSent,
          terminalEventsSent,
          documentToken,
        }),
      };
    },
    { targetEventPath: eventPath, draftText: LONG_DRAFT },
  );

  let targetPollResponses = 0;
  const targetPollStatuses: string[] = [];
  const pollRoute = async (route: Route) => {
    if (!isTargetGet(route, pollPath)) {
      await route.continue();
      return;
    }
    const response = {
      runId: fixture.activeRunId,
      threadId: fixture.threadId,
      status: "running",
      errorCode: null,
      progressStage: "synthesizing",
      attemptEpoch: 1,
      recentEvents: [],
    } satisfies ResearchRunResponse;
    targetPollResponses += 1;
    targetPollStatuses.push(response.status);
    await route.fulfill({
      status: 200,
      headers: { "Cache-Control": "no-store" },
      json: response,
    });
  };
  await page.route("**/api/research/runs/**", pollRoute);

  let gateArmed = false;
  let gateReleased = false;
  let terminalRscRequests = 0;
  let terminalMainFrameNavigations = 0;
  let resolveGateRequest!: () => void;
  const gateRequest = new Promise<void>((resolve) => {
    resolveGateRequest = resolve;
  });
  let resolveGateRelease!: () => void;
  const gateRelease = new Promise<void>((resolve) => {
    resolveGateRelease = resolve;
  });
  const rscRoute = async (route: Route) => {
    const request = route.request();
    const headers = request.headers();
    const url = new URL(request.url());
    if (
      !gateArmed ||
      request.method() !== "GET" ||
      url.pathname !== threadPath ||
      headers.rsc !== "1" ||
      isPrefetch(headers)
    ) {
      await route.continue();
      return;
    }
    terminalRscRequests += 1;
    resolveGateRequest();
    await gateRelease;
    await route.continue();
  };
  await page.route(`**${threadPath}*`, rscRoute);

  const documentNavigationListener = (request: Request) => {
    if (
      gateArmed &&
      request.isNavigationRequest() &&
      request.frame() === page.mainFrame()
    ) {
      terminalMainFrameNavigations += 1;
    }
  };
  page.on("request", documentNavigationListener);

  async function eventSourceStats(): Promise<BrowserEventSourceStats> {
    return page.evaluate(() => {
      const bridge = (window as ContinuityWindow).__researchContinuity;
      if (bridge === undefined) {
        throw new Error("Research continuity browser bridge is not installed");
      }
      return bridge.getEventSourceStats();
    });
  }

  async function emitDraft(): Promise<void> {
    await page.waitForFunction(() => {
      const bridge = (window as ContinuityWindow).__researchContinuity;
      return (bridge?.getEventSourceStats().eventSourcesCreated ?? 0) >= 1;
    });
    await page.evaluate(() => {
      const bridge = (window as ContinuityWindow).__researchContinuity;
      if (bridge === undefined) throw new Error("Continuity bridge is missing");
      bridge.emitDraft();
    });
  }

  async function emitFailedTerminal(): Promise<void> {
    await page.evaluate(() => {
      const bridge = (window as ContinuityWindow).__researchContinuity;
      if (bridge === undefined) throw new Error("Continuity bridge is missing");
      bridge.emitFailure();
    });
  }

  async function startSampler(): Promise<ResearchContinuityPaintSample> {
    return page.evaluate((runId) => {
      const continuityWindow = window as ContinuityWindow;
      const bridge = continuityWindow.__researchContinuity;
      if (bridge === undefined) throw new Error("Continuity bridge is missing");
      const installedBridge = bridge;
      const selector = `[data-research-run-id="${CSS.escape(runId)}"]`;
      const initialTurn = activeTurn();
      const initialThreadPanel =
        initialTurn?.closest<HTMLElement>("main") ?? null;
      const initialComposer = activeComposer(initialThreadPanel);
      const initialAnswerScroller =
        initialThreadPanel?.querySelector<HTMLElement>(
          "[data-research-answer-scroll-region]",
        ) ?? null;
      if (
        initialTurn === null ||
        initialThreadPanel === null ||
        initialComposer === undefined ||
        initialComposer === null ||
        initialAnswerScroller === null
      ) {
        throw new Error("Continuity sampler target is missing");
      }
      const initialFocus = document.activeElement;
      const initialSourceTrigger = sourceTrigger(initialThreadPanel);
      const initialSourceScroller = sourceScroller(initialSourceTrigger);
      let failureRail: Element | null = null;
      let done = false;
      let mutationObserved = true;
      const samples: ResearchContinuityPaintSample[] = [];

      function rect(element: Element): ResearchContinuityRect {
        const box = element.getBoundingClientRect();
        return {
          x: box.x,
          y: box.y,
          width: box.width,
          height: box.height,
        };
      }

      function activeTurn(): HTMLElement | null {
        const candidates = Array.from(
          document.querySelectorAll<HTMLElement>(selector),
        ).filter((candidate) => {
          const panel = candidate.closest<HTMLElement>("main");
          return (
            candidate.getClientRects().length > 0 &&
            panel !== null &&
            panel.getClientRects().length > 0
          );
        });
        return candidates.length === 1 ? (candidates[0] ?? null) : null;
      }

      function sourceSurface(
        trigger: HTMLButtonElement | null,
      ): HTMLElement | null {
        const controlledId = trigger?.getAttribute("aria-controls");
        if (controlledId === undefined || controlledId === null) return null;
        const owner = trigger?.closest<HTMLElement>("main");
        const ownedSurface = owner?.querySelector<HTMLElement>(
          `#${CSS.escape(controlledId)}`,
        );
        if (ownedSurface?.getClientRects().length) return ownedSurface;
        const visibleSurfaces = Array.from(
          document.querySelectorAll<HTMLElement>(
            `#${CSS.escape(controlledId)}`,
          ),
        ).filter((surface) => surface.getClientRects().length > 0);
        return visibleSurfaces.length === 1
          ? (visibleSurfaces[0] ?? null)
          : null;
      }

      function sourceScroller(
        trigger: HTMLButtonElement | null,
      ): HTMLElement | null {
        const surface = sourceSurface(trigger);
        return surface?.lastElementChild instanceof HTMLElement
          ? surface.lastElementChild
          : null;
      }

      function sourceTrigger(panel: HTMLElement): HTMLButtonElement | null {
        return (
          Array.from(
            panel.querySelectorAll<HTMLButtonElement>("button[aria-expanded]"),
          ).find((button) => button.textContent?.includes("ソース") === true) ??
          null
        );
      }

      function activeComposer(
        panel: HTMLElement | null,
      ): HTMLFormElement | null {
        if (panel === null) return null;
        const textarea = Array.from(
          panel.querySelectorAll<HTMLElement>("#research-question"),
        ).find((element) => element.getClientRects().length > 0);
        return textarea?.closest<HTMLFormElement>("form") ?? null;
      }

      function record(): void {
        if (done) return;
        const turn = activeTurn();
        const threadPanel = turn?.closest<HTMLElement>("main") ?? null;
        const composer = activeComposer(threadPanel);
        const answerScroller =
          threadPanel?.querySelector<HTMLElement>(
            "[data-research-answer-scroll-region]",
          ) ?? null;
        if (
          turn === null ||
          threadPanel === null ||
          composer === undefined ||
          composer === null ||
          answerScroller === null
        ) {
          throw new Error("Continuity sampler target disappeared");
        }
        const failureRails = turn.querySelectorAll(
          "[data-research-failure-rail]",
        );
        if (failureRail === null && failureRails.length === 1) {
          failureRail = failureRails.item(0);
        }
        const trigger = sourceTrigger(threadPanel);
        const currentSourceSurface = sourceSurface(trigger);
        const currentSourceScroller = sourceScroller(trigger);
        const activeElement = document.activeElement;
        const protectedRootLoadingCount = Array.from(
          threadPanel.querySelectorAll('[role="status"]'),
        ).filter(
          (element) =>
            element.textContent?.includes("記事を読み込み中") &&
            element.getClientRects().length > 0,
        ).length;
        const sample: ResearchContinuityPaintSample = {
          timestamp: performance.now(),
          mutationObserved,
          documentToken: installedBridge.documentToken,
          persistedStatus: turn.getAttribute("data-research-persisted-status"),
          draftCount: turn.querySelectorAll(
            '[data-testid="research-answer-slot"]',
          ).length,
          failureCount: failureRails.length,
          protectedLoadingCount:
            Array.from(
              threadPanel.querySelectorAll(
                '[data-testid="research-navigation-overlay"]',
              ),
            ).filter((element) => element.getClientRects().length > 0).length +
            protectedRootLoadingCount,
          announcerCount: threadPanel.querySelectorAll(
            '[role="status"][aria-live="polite"][aria-atomic="true"]',
          ).length,
          sourceSurfaceCount: currentSourceSurface === null ? 0 : 1,
          sourcesExpanded: trigger?.getAttribute("aria-expanded") ?? null,
          sourcesControls: trigger?.getAttribute("aria-controls") ?? null,
          sourceScrollTop: currentSourceScroller?.scrollTop ?? null,
          focusedHref:
            activeElement instanceof HTMLAnchorElement
              ? activeElement.getAttribute("href")
              : null,
          sameFocus: activeElement === initialFocus,
          sameTurn: turn === initialTurn,
          sameFailureRail:
            failureRail === null
              ? null
              : failureRails.length === 1 &&
                failureRails.item(0) === failureRail,
          sameThreadPanel: threadPanel === initialThreadPanel,
          sameComposer: composer === initialComposer,
          sameAnswerScroller: answerScroller === initialAnswerScroller,
          sameSourceScroller:
            initialSourceScroller === null
              ? currentSourceScroller === null
              : currentSourceScroller === initialSourceScroller,
          turnRect: rect(turn),
          threadPanelRect: rect(threadPanel),
          composerRect: rect(composer),
          answerScrollTop: answerScroller.scrollTop,
          answerScrollHeight: answerScroller.scrollHeight,
          answerClientHeight: answerScroller.clientHeight,
        };
        mutationObserved = false;
        samples.push(sample);
        if (sample.persistedStatus === "failed") {
          done = true;
          observer.disconnect();
          installedBridge.sampler = { done: true, samples };
        }
      }

      function sampleNextPaint(): void {
        if (done) return;
        requestAnimationFrame(() => {
          record();
          sampleNextPaint();
        });
      }

      const observer = new MutationObserver(() => {
        mutationObserved = true;
      });
      observer.observe(document.body, {
        attributes: true,
        childList: true,
        subtree: true,
      });
      record();
      sampleNextPaint();
      installedBridge.sampler = { done, samples };
      return samples[0] as ResearchContinuityPaintSample;
    }, fixture.activeRunId);
  }

  async function waitForPersistedSample(): Promise<void> {
    await page.waitForFunction(() => {
      return (window as ContinuityWindow).__researchContinuity?.sampler?.done;
    });
  }

  async function samples(): Promise<ResearchContinuityPaintSample[]> {
    return page.evaluate(() => {
      const sampler = (window as ContinuityWindow).__researchContinuity
        ?.sampler;
      if (sampler === undefined)
        throw new Error("Continuity sampler is missing");
      return sampler.samples;
    });
  }

  async function stats(): Promise<ResearchContinuityHarnessStats> {
    const browserStats = await eventSourceStats();
    return {
      targetPollResponses,
      targetPollStatuses: [...targetPollStatuses],
      terminalRscRequests,
      terminalMainFrameNavigations,
      ...browserStats,
    };
  }

  function armTerminalRefreshGate(): void {
    gateArmed = true;
  }

  function releaseTerminalRefresh(): void {
    if (gateReleased) return;
    gateReleased = true;
    resolveGateRelease();
  }

  async function cleanup(): Promise<void> {
    releaseTerminalRefresh();
    page.off("request", documentNavigationListener);
    await page.unroute("**/api/research/runs/**", pollRoute);
    await page.unroute(`**${threadPath}*`, rscRoute);
  }

  return {
    emitDraft,
    startSampler,
    armTerminalRefreshGate,
    emitFailedTerminal,
    waitForTerminalRefresh: () => gateRequest,
    releaseTerminalRefresh,
    waitForPersistedSample,
    samples,
    stats,
    cleanup,
  };
}
