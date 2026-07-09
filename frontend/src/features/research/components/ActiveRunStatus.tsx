"use client";

import { Loader2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import type { ResearchRunResponse } from "@/types/types.gen";

const BASE_DELAY_MS = 2000;
const MAX_DELAY_MS = 10000;

type ActiveRunStatusValue = Extract<
  ResearchRunResponse["status"],
  "queued" | "running"
>;
type ProgressStage = ResearchRunResponse["progressStage"];

interface ActiveRunStatusProps {
  runId: string;
  initialStatus: ActiveRunStatusValue;
  initialStage: ProgressStage;
}

interface RunSignal {
  status: ResearchRunResponse["status"];
  progressStage: ProgressStage;
}

function activeRunText(signal: RunSignal): string | null {
  if (signal.status === "queued") return "待機中";
  if (signal.status !== "running") return null;
  switch (signal.progressStage) {
    case "planning":
      return "計画中";
    case "retrieving":
      return "情報収集中";
    case "synthesizing":
      return "回答作成中";
    default:
      return "生成中";
  }
}

export function ActiveRunStatus({
  runId,
  initialStatus,
  initialStage,
}: ActiveRunStatusProps) {
  const router = useRouter();
  const [signal, setSignal] = useState<RunSignal>({
    status: initialStatus,
    progressStage: initialStage,
  });

  useEffect(() => {
    setSignal((current) => {
      if (
        current.status === initialStatus &&
        current.progressStage === initialStage
      ) {
        return current;
      }
      return { status: initialStatus, progressStage: initialStage };
    });
  }, [initialStatus, initialStage]);

  useEffect(() => {
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
        if (stopped) return;
        delayMs = BASE_DELAY_MS;
        if (data.status === "completed" || data.status === "failed") {
          router.refresh();
          return;
        }
        setSignal({ status: data.status, progressStage: data.progressStage });
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

  const text = activeRunText(signal);
  if (text === null) return null;

  return (
    <div
      className="mt-2 flex min-w-0 items-center gap-1.5 text-xs text-[var(--vector-ink-muted)]"
      role="status"
      aria-live="polite"
    >
      <Loader2 aria-hidden="true" className="size-3.5 shrink-0 animate-spin" />
      <span className="min-w-0 break-words [overflow-wrap:anywhere]">
        {text}
      </span>
    </div>
  );
}
