import { AlertTriangle, Bot, Loader2 } from "lucide-react";
import type { ResearchRunResponse } from "@/types/types.gen";
import type { ResearchLiveDraftMode } from "../live/reducer";

interface LiveAnswerDraftProps {
  status: ResearchRunResponse["status"];
  draftMode: ResearchLiveDraftMode;
  draftText: string;
  errorCode: ResearchRunResponse["errorCode"];
}

function failureText(errorCode: ResearchRunResponse["errorCode"]): string {
  switch (errorCode) {
    case "cancelled":
      return "キャンセルしました";
    case "enqueue_failed":
      return "実行キューに投入できませんでした";
    case "stale":
      return "時間切れになりました";
    default:
      return "回答を生成できませんでした";
  }
}

export function LiveAnswerDraft({
  status,
  draftMode,
  draftText,
  errorCode,
}: LiveAnswerDraftProps) {
  if (
    status === "queued" ||
    (status === "running" &&
      (draftMode !== "visible" || draftText.length === 0))
  ) {
    return null;
  }

  if (status === "failed") {
    return (
      <article className="flex min-w-0 gap-3">
        <div className="mt-1 flex size-8 shrink-0 items-center justify-center rounded-md bg-[var(--vector-paper)] text-[var(--vector-ink-muted)] ring-1 ring-inset ring-[var(--vector-rule)]">
          <Bot aria-hidden="true" className="size-4" />
        </div>
        <div className="min-w-0 flex-1 pt-1.5">
          <div
            className="flex min-w-0 items-center gap-1.5 text-xs font-medium text-[var(--vector-ink-muted)]"
            role="status"
            aria-live="polite"
            aria-atomic="true"
          >
            <AlertTriangle aria-hidden="true" className="size-3.5 shrink-0" />
            <span className="min-w-0 break-words [overflow-wrap:anywhere]">
              {failureText(errorCode)}
            </span>
          </div>
        </div>
      </article>
    );
  }

  const isFinalizing = status === "completed";
  const showsDraft = draftMode === "visible" && draftText.length > 0;

  return (
    <article className="flex min-w-0 gap-3" aria-busy="true">
      <div className="mt-1 flex size-8 shrink-0 items-center justify-center rounded-md bg-[var(--vector-accent-tint)] text-[var(--vector-accent-ink)]">
        <Bot aria-hidden="true" className="size-4" />
      </div>
      <div className="min-w-0 flex-1">
        <div
          className="mb-2 flex min-w-0 items-center gap-1.5 text-[11px] font-semibold tracking-[0.04em] text-[var(--vector-accent-ink)]"
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          <Loader2
            aria-hidden="true"
            className="size-3.5 shrink-0 animate-spin motion-reduce:animate-none"
          />
          <span>
            {isFinalizing ? "回答を確定しています…" : "回答を生成中…"}
          </span>
        </div>
        {showsDraft ? (
          <p className="whitespace-pre-wrap break-words text-sm leading-7 text-[var(--vector-ink)] [overflow-wrap:anywhere]">
            {draftText}
          </p>
        ) : null}
      </div>
    </article>
  );
}
