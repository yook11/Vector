"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import type { ResearchRunResponse } from "@/types/types.gen";

const BASE_DELAY_MS = 2000;
const MAX_DELAY_MS = 10000;

interface ResearchRunPollerProps {
  runId: string | null;
}

export function ResearchRunPoller({ runId }: ResearchRunPollerProps) {
  const router = useRouter();

  useEffect(() => {
    if (!runId) return;

    let stopped = false;
    let delayMs = BASE_DELAY_MS;
    let timeout: ReturnType<typeof setTimeout> | undefined;

    function clearTimer() {
      if (timeout !== undefined) {
        clearTimeout(timeout);
        timeout = undefined;
      }
    }

    function schedule() {
      clearTimer();
      if (!stopped && !document.hidden) {
        timeout = setTimeout(poll, delayMs);
      }
    }

    async function poll() {
      if (stopped || document.hidden) return;
      try {
        const response = await fetch(`/api/research/runs/${runId}`, {
          cache: "no-store",
        });
        if (stopped) return;
        if ([401, 403, 404].includes(response.status)) {
          router.refresh();
          return;
        }
        if (!response.ok) {
          delayMs = Math.min(delayMs * 2, MAX_DELAY_MS);
          schedule();
          return;
        }

        const data = (await response.json()) as ResearchRunResponse;
        delayMs = BASE_DELAY_MS;
        if (data.status === "completed" || data.status === "failed") {
          router.refresh();
          return;
        }
        schedule();
      } catch {
        if (stopped) return;
        delayMs = Math.min(delayMs * 2, MAX_DELAY_MS);
        schedule();
      }
    }

    function handleVisibilityChange() {
      if (document.hidden) {
        clearTimer();
        return;
      }
      delayMs = BASE_DELAY_MS;
      void poll();
    }

    document.addEventListener("visibilitychange", handleVisibilityChange);
    void poll();

    return () => {
      stopped = true;
      clearTimer();
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [runId, router]);

  return null;
}
