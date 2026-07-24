"use client";

import { Loader2 } from "lucide-react";
import { useResearchSubmission } from "./ResearchSubmissionBoundary";

export function ResearchSubmissionStatus() {
  const { isSubmissionPending } = useResearchSubmission();

  if (!isSubmissionPending) return null;

  return (
    <div
      role="status"
      aria-label="質問を送信しています…"
      aria-live="polite"
      aria-atomic="true"
      className="mx-auto flex w-full max-w-[860px] items-center gap-3 rounded-md border border-[var(--vector-rule)] bg-[var(--vector-surface)] px-4 py-3 text-sm font-medium text-[var(--vector-ink)]"
    >
      <Loader2
        aria-hidden="true"
        className="size-4 shrink-0 animate-spin motion-reduce:animate-none"
      />
      <p>質問を送信しています…</p>
    </div>
  );
}
