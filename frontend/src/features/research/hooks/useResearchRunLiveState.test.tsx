import {
  act,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { type ReactNode, StrictMode, Suspense, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createInitialResearchLiveState } from "../live/reducer";
import { useResearchRunLiveState } from "./useResearchRunLiveState";

const RUN_ONE = "00000000-0000-4000-a000-000000000010";
const RUN_TWO = "00000000-0000-4000-a000-000000000020";
const CREATED_AT_ONE = "2026-07-22T12:00:00.000Z";
const CREATED_AT_TWO = "2026-07-22T12:00:01.000Z";

interface ControllerOptions {
  runId: string;
  createdAt?: string;
  requestRefresh?: () => Promise<void>;
  refresh?: () => void;
}

interface MockController {
  subscribe: (listener: () => void) => () => void;
  getSnapshot: () => ReturnType<typeof snapshot>;
}

const mocks = vi.hoisted(() => ({
  createController: vi.fn(),
  refresh: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: mocks.refresh }),
}));

vi.mock("../live/controller", () => ({
  createResearchRunLiveController: mocks.createController,
}));

function snapshot() {
  return {
    runStatus: "running" as const,
    connectionMode: "connecting" as const,
    liveState: createInitialResearchLiveState(),
  };
}

function liveStateInput(runId: string, createdAt = CREATED_AT_ONE) {
  return {
    runId,
    createdAt,
    initialStatus: "running" as const,
    initialStage: null,
  } as unknown as Parameters<typeof useResearchRunLiveState>[0];
}

function strictMode({ children }: { children: ReactNode }) {
  return <StrictMode>{children}</StrictMode>;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

describe("useResearchRunLiveState", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("keeps at most one active controller subscription in React StrictMode", () => {
    let activeSubscriptions = 0;
    let maximumActiveSubscriptions = 0;
    mocks.createController.mockImplementation(
      (): MockController => ({
        getSnapshot: snapshot,
        subscribe: () => {
          activeSubscriptions += 1;
          maximumActiveSubscriptions = Math.max(
            maximumActiveSubscriptions,
            activeSubscriptions,
          );
          return () => {
            activeSubscriptions -= 1;
          };
        },
      }),
    );

    const { unmount } = renderHook(
      () => useResearchRunLiveState(liveStateInput(RUN_ONE)),
      { wrapper: strictMode },
    );

    expect(maximumActiveSubscriptions).toBe(1);
    expect(activeSubscriptions).toBe(1);

    unmount();
    expect(activeSubscriptions).toBe(0);
  });

  it("uses runId and persisted createdAt as controller identity", () => {
    let activeSubscriptions = 0;
    let maximumActiveSubscriptions = 0;
    const createdIdentities: string[] = [];
    mocks.createController.mockImplementation(
      (options: ControllerOptions): MockController => {
        createdIdentities.push(`${options.runId}:${options.createdAt}`);
        return {
          getSnapshot: snapshot,
          subscribe: () => {
            activeSubscriptions += 1;
            maximumActiveSubscriptions = Math.max(
              maximumActiveSubscriptions,
              activeSubscriptions,
            );
            return () => {
              activeSubscriptions -= 1;
            };
          },
        };
      },
    );

    const { rerender, unmount } = renderHook(
      ({ runId, createdAt }) =>
        useResearchRunLiveState(liveStateInput(runId, createdAt)),
      { initialProps: { runId: RUN_ONE, createdAt: CREATED_AT_ONE } },
    );

    rerender({ runId: RUN_ONE, createdAt: CREATED_AT_ONE });
    expect(createdIdentities).toEqual([`${RUN_ONE}:${CREATED_AT_ONE}`]);

    rerender({ runId: RUN_ONE, createdAt: CREATED_AT_TWO });
    expect(createdIdentities).toEqual([
      `${RUN_ONE}:${CREATED_AT_ONE}`,
      `${RUN_ONE}:${CREATED_AT_TWO}`,
    ]);
    expect(activeSubscriptions).toBe(1);

    rerender({ runId: RUN_TWO, createdAt: CREATED_AT_TWO });
    expect(createdIdentities).toEqual([
      `${RUN_ONE}:${CREATED_AT_ONE}`,
      `${RUN_ONE}:${CREATED_AT_TWO}`,
      `${RUN_TWO}:${CREATED_AT_TWO}`,
    ]);
    expect(maximumActiveSubscriptions).toBe(1);
    expect(activeSubscriptions).toBe(1);

    unmount();
    expect(activeSubscriptions).toBe(0);
  });

  it("returns the controller snapshot without starting a second lifecycle", () => {
    const current = snapshot();
    mocks.createController.mockReturnValue({
      getSnapshot: () => current,
      subscribe: () => () => undefined,
    } satisfies MockController);

    const { result, unmount } = renderHook(() =>
      useResearchRunLiveState(liveStateInput(RUN_ONE)),
    );

    expect(result.current).toBe(current);
    expect(mocks.createController).toHaveBeenCalledTimes(1);

    unmount();
  });

  it("acknowledges one StrictMode refresh only after its transition commits", async () => {
    const payloadCommit = deferred<void>();
    const controllerOptions: ControllerOptions[] = [];
    let payloadReady = false;
    let refreshRoutePayload: (() => void) | null = null;
    mocks.createController.mockImplementation(
      (options: ControllerOptions): MockController => {
        controllerOptions.push(options);
        return {
          getSnapshot: snapshot,
          subscribe: () => () => undefined,
        };
      },
    );
    mocks.refresh.mockImplementation(() => refreshRoutePayload?.());

    function RoutePayload({ version }: { version: number }) {
      if (version === 1 && !payloadReady) throw payloadCommit.promise;
      return <span data-testid="route-payload">{version}</span>;
    }

    function Harness() {
      const [version, setVersion] = useState(0);
      refreshRoutePayload = () => setVersion(1);
      useResearchRunLiveState(liveStateInput(RUN_ONE));
      return (
        <Suspense fallback={<span>loading</span>}>
          <RoutePayload version={version} />
        </Suspense>
      );
    }

    const view = render(
      <StrictMode>
        <Harness />
      </StrictMode>,
    );
    const options = controllerOptions.at(-1);
    expect(options).toBeDefined();
    expect(options).not.toHaveProperty("refresh");
    expect(options?.requestRefresh).toEqual(expect.any(Function));
    const requestRefresh = options?.requestRefresh;
    if (requestRefresh === undefined) {
      throw new Error("requestRefresh port is missing");
    }

    let acknowledged = false;
    let firstRequest!: Promise<void>;
    act(() => {
      firstRequest = requestRefresh();
      const duplicateRequest = requestRefresh();
      void duplicateRequest.catch(() => undefined);
      void firstRequest.then(() => {
        acknowledged = true;
      });
    });

    expect(mocks.refresh).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("route-payload")).toHaveTextContent("0");
    await act(async () => Promise.resolve());
    expect(acknowledged).toBe(false);

    payloadReady = true;
    await act(async () => {
      payloadCommit.resolve(undefined);
      await payloadCommit.promise;
    });
    await waitFor(() => expect(acknowledged).toBe(true));
    await firstRequest;

    expect(screen.getByTestId("route-payload")).toHaveTextContent("1");
    expect(mocks.refresh).toHaveBeenCalledTimes(1);
    view.unmount();
  });
});
