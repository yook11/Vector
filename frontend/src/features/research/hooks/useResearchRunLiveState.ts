"use client";

import { useRouter } from "next/navigation";
import {
  useCallback,
  useEffect,
  useRef,
  useSyncExternalStore,
  useTransition,
} from "react";
import {
  createResearchRunLiveController,
  type ResearchRunLiveController,
  type ResearchRunLiveSnapshot,
  type ResearchRunLiveStatus,
} from "../live/controller";
import type { ResearchLiveStage } from "../live/events";

interface UseResearchRunLiveStateInput {
  runId: string;
  createdAt: string;
  initialStatus: Extract<ResearchRunLiveStatus, "queued" | "running">;
  initialStage: ResearchLiveStage | null;
}

interface ControllerEntry {
  runId: string;
  createdAt: string;
  subscribe: ResearchRunLiveController["subscribe"];
  getSnapshot: ResearchRunLiveController["getSnapshot"];
}

interface RefreshCommitRequest {
  promise: Promise<void>;
  resolve: () => void;
  observedPending: boolean;
}

export function useResearchRunLiveState({
  runId,
  createdAt,
  initialStatus,
  initialStage,
}: UseResearchRunLiveStateInput): ResearchRunLiveSnapshot {
  const router = useRouter();
  const routerRef = useRef(router);
  const refreshRequestRef = useRef<RefreshCommitRequest | null>(null);
  const [isRefreshPending, startRefreshTransition] = useTransition();
  const controllerRef = useRef<ControllerEntry | null>(null);

  useEffect(() => {
    routerRef.current = router;
  }, [router]);

  useEffect(() => {
    const request = refreshRequestRef.current;
    if (request === null) return;
    if (isRefreshPending) {
      request.observedPending = true;
      return;
    }
    if (!request.observedPending) return;
    refreshRequestRef.current = null;
    request.resolve();
  }, [isRefreshPending]);

  const requestRefresh = useCallback((): Promise<void> => {
    const activeRequest = refreshRequestRef.current;
    if (activeRequest !== null) return activeRequest.promise;

    let resolve!: () => void;
    const promise = new Promise<void>((resolvePromise) => {
      resolve = resolvePromise;
    });
    refreshRequestRef.current = {
      promise,
      resolve,
      observedPending: false,
    };
    startRefreshTransition(() => routerRef.current.refresh());
    return promise;
  }, []);

  if (
    controllerRef.current?.runId !== runId ||
    controllerRef.current.createdAt !== createdAt
  ) {
    const controller = createResearchRunLiveController({
      runId,
      createdAt,
      initialStatus,
      initialStage,
      requestRefresh,
    });
    let cachedSnapshot = controller.getSnapshot();
    controllerRef.current = {
      runId,
      createdAt,
      getSnapshot: () => cachedSnapshot,
      subscribe: (listener) =>
        controller.subscribe(() => {
          const nextSnapshot = controller.getSnapshot();
          if (Object.is(nextSnapshot, cachedSnapshot)) return;
          cachedSnapshot = nextSnapshot;
          listener();
        }),
    };
  }

  const controller = controllerRef.current;
  return useSyncExternalStore(
    controller.subscribe,
    controller.getSnapshot,
    controller.getSnapshot,
  );
}
