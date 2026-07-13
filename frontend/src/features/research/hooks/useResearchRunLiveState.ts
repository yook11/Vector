"use client";

import { useRouter } from "next/navigation";
import { useRef, useSyncExternalStore } from "react";
import {
  createResearchRunLiveController,
  type ResearchRunLiveController,
  type ResearchRunLiveSnapshot,
  type ResearchRunLiveStatus,
} from "../live/controller";
import type { ResearchLiveStage } from "../live/events";

interface UseResearchRunLiveStateInput {
  runId: string;
  initialStatus: Extract<ResearchRunLiveStatus, "queued" | "running">;
  initialStage: ResearchLiveStage | null;
}

interface ControllerEntry {
  runId: string;
  subscribe: ResearchRunLiveController["subscribe"];
  getSnapshot: ResearchRunLiveController["getSnapshot"];
}

export function useResearchRunLiveState({
  runId,
  initialStatus,
  initialStage,
}: UseResearchRunLiveStateInput): ResearchRunLiveSnapshot {
  const router = useRouter();
  const controllerRef = useRef<ControllerEntry | null>(null);

  if (controllerRef.current?.runId !== runId) {
    const controller = createResearchRunLiveController({
      runId,
      initialStatus,
      initialStage,
      refresh: () => router.refresh(),
    });
    let cachedSnapshot = controller.getSnapshot();
    controllerRef.current = {
      runId,
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
