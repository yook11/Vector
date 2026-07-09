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
  recentEvents: readonly unknown[];
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function numericField(
  event: Record<string, unknown>,
  key: string,
): number | null {
  const value = event[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function latestKnownEventText(events: readonly unknown[]): string | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const text = liveEventText(events[index]);
    if (text !== null) return text;
  }
  return null;
}

function liveEventText(event: unknown): string | null {
  if (!isRecord(event) || typeof event.type !== "string") return null;

  switch (event.type) {
    case "internal_search.started":
      return "関連記事を検索中";
    case "internal_search.completed": {
      const count = numericField(event, "hitCount");
      return count === null ? null : `関連記事${count}件を確認`;
    }
    case "external_search.queries_generated": {
      if (!Array.isArray(event.queries)) return null;
      const queries = event.queries.filter(
        (query): query is string =>
          typeof query === "string" && query.length > 0,
      );
      if (queries.length === 0) return null;
      if (queries.length === 1) return `“${queries[0]}” を検索中`;
      return `“${queries[0]}” など${queries.length}件を検索中`;
    }
    case "external_search.candidates_fetched": {
      const count = numericField(event, "candidateCount");
      return count === null ? null : `候補${count}件を取得`;
    }
    case "external_search.evidence_selected": {
      const count = numericField(event, "evidenceCount");
      return count === null ? null : `根拠${count}件を選別`;
    }
    default:
      return null;
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
    recentEvents: [],
  });

  useEffect(() => {
    setSignal((current) => {
      if (
        current.status === initialStatus &&
        current.progressStage === initialStage
      ) {
        return current;
      }
      return {
        status: initialStatus,
        progressStage: initialStage,
        recentEvents: [],
      };
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
        setSignal({
          status: data.status,
          progressStage: data.progressStage,
          recentEvents: Array.isArray(data.recentEvents)
            ? data.recentEvents
            : [],
        });
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
  const eventText =
    signal.status === "running" && signal.progressStage === "retrieving"
      ? latestKnownEventText(signal.recentEvents)
      : null;

  return (
    <div
      className="mt-2 flex min-w-0 items-start gap-1.5 text-xs text-[var(--vector-ink-muted)]"
      role="status"
      aria-live="polite"
    >
      <Loader2
        aria-hidden="true"
        className="mt-px size-3.5 shrink-0 animate-spin"
      />
      <span className="flex min-w-0 flex-col gap-0.5">
        <span className="min-w-0 break-words [overflow-wrap:anywhere]">
          {text}
        </span>
        {eventText ? (
          <span className="min-w-0 break-words text-[11px] leading-4 text-[var(--vector-ink-soft)] [overflow-wrap:anywhere]">
            {eventText}
          </span>
        ) : null}
      </span>
    </div>
  );
}
