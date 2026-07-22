import { AlertTriangle, Bot, Loader2 } from "lucide-react";
import type { ResearchRunResponse } from "@/types/types.gen";
import type { ResearchLiveDraftMode } from "../live/reducer";

interface LiveAnswerDraftProps {
  status: ResearchRunResponse["status"];
  draftMode: ResearchLiveDraftMode;
  draftText: string;
  errorCode: ResearchRunResponse["errorCode"];
}

interface LiveAnswerDraftContentProps extends LiveAnswerDraftProps {
  isRecoveryPending: boolean;
}

export function failureText(
  errorCode: ResearchRunResponse["errorCode"],
): string {
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

function FailureContent({
  errorCode,
}: Pick<LiveAnswerDraftContentProps, "errorCode">) {
  return (
    <div className="flex min-w-0 items-center gap-1.5 text-xs font-medium text-[var(--vector-ink-muted)]">
      <AlertTriangle aria-hidden="true" className="size-3.5 shrink-0" />
      <span className="min-w-0 break-words [overflow-wrap:anywhere]">
        {failureText(errorCode)}
      </span>
    </div>
  );
}

function DraftContent({
  status,
  draftMode,
  draftText,
  isRecoveryPending,
}: Pick<
  LiveAnswerDraftContentProps,
  "status" | "draftMode" | "draftText" | "isRecoveryPending"
>) {
  const isFinalizing = status === "completed";
  const showsDraft = draftMode === "visible" && draftText.length > 0;
  const statusText = isFinalizing
    ? "回答を確定しています…"
    : isRecoveryPending
      ? "回答の状態を確認しています…"
      : "回答を生成中…";

  return (
    <>
      <div className="mb-2 flex min-w-0 items-center gap-1.5 text-[11px] font-semibold tracking-[0.04em] text-[var(--vector-accent-ink)]">
        <Loader2
          aria-hidden="true"
          className="size-3.5 shrink-0 animate-spin motion-reduce:animate-none"
        />
        <span>{statusText}</span>
      </div>
      {showsDraft ? (
        <p className="whitespace-pre-wrap break-words text-sm leading-7 text-[var(--vector-ink)] [overflow-wrap:anywhere]">
          {draftText}
        </p>
      ) : null}
    </>
  );
}

export function LiveAnswerSlotContent({
  status,
  draftMode,
  draftText,
  errorCode,
  isRecoveryPending,
}: LiveAnswerDraftContentProps) {
  if (status === "failed") {
    return <FailureContent errorCode={errorCode} />;
  }
  if (draftMode === "visible" && draftText.length > 0) {
    return (
      <DraftContent
        status={status}
        draftMode={draftMode}
        draftText={draftText}
        isRecoveryPending={isRecoveryPending}
      />
    );
  }
  if (status === "completed") {
    return (
      <p className="text-sm leading-6 text-[var(--vector-ink-muted)]">
        回答を確定しています…
      </p>
    );
  }
  return (
    <p className="text-sm leading-6 text-[var(--vector-ink-muted)]">
      {draftMode === "suppressed" || isRecoveryPending
        ? "回答の状態を確認しています…"
        : "回答を準備しています…"}
    </p>
  );
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
          <FailureContent errorCode={errorCode} />
        </div>
      </article>
    );
  }

  return (
    <article className="flex min-w-0 gap-3" aria-busy="true">
      <div className="mt-1 flex size-8 shrink-0 items-center justify-center rounded-md bg-[var(--vector-accent-tint)] text-[var(--vector-accent-ink)]">
        <Bot aria-hidden="true" className="size-4" />
      </div>
      <div className="min-w-0 flex-1">
        <DraftContent
          status={status}
          draftMode={draftMode}
          draftText={draftText}
          isRecoveryPending={false}
        />
      </div>
    </article>
  );
}
