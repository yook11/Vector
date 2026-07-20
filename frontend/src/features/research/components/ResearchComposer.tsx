"use client";

import { Loader2, Send, Square } from "lucide-react";
import { useRouter } from "next/navigation";
import { type FormEvent, useState, useTransition } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { isRedirectError } from "@/lib/utils/redirect-error";
import { toastError } from "@/lib/utils/toast-error";
import { cancelResearchRun } from "../api/cancel-research-run";
import { submitResearchQuestion } from "../api/submit-research-question";
import { useResearchNavigation } from "./ResearchNavigationBoundary";

interface ResearchComposerProps {
  threadId?: string;
  activeRunId: string | null;
}

export function ResearchComposer({
  threadId,
  activeRunId,
}: ResearchComposerProps) {
  const { isNavigationPending } = useResearchNavigation();
  const router = useRouter();
  const [question, setQuestion] = useState("");
  const [pending, startTransition] = useTransition();
  const disabled = pending || activeRunId !== null || isNavigationPending;

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (disabled) return;
    const nextQuestion = question.trim();
    if (!nextQuestion) return;

    startTransition(async () => {
      try {
        const result = await submitResearchQuestion(nextQuestion, threadId);
        if (result.kind === "daily-request-limit-exceeded") {
          toast.error(
            result.retryAfterSeconds > 0
              ? "本日の利用上限（10回）に達しました。未開始のリクエストを停止すると、その分を再度利用できます。利用枠は日本時間の翌日0:00にリセットされます"
              : "利用枠がリセットされました。もう一度お試しください",
          );
          return;
        }
        setQuestion("");
      } catch (err) {
        if (isRedirectError(err)) throw err;
        toastError(err, "質問を送信できませんでした");
      }
    });
  }

  function handleCancel() {
    if (!activeRunId) return;
    startTransition(async () => {
      try {
        await cancelResearchRun(activeRunId, threadId);
        router.refresh();
      } catch (err) {
        if (isRedirectError(err)) throw err;
        toastError(err, "実行を停止できませんでした");
      }
    });
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="min-w-0 shrink-0 border-t border-[var(--vector-rule)] bg-[var(--vector-surface)]/92 px-3 pt-3 pb-[calc(0.75rem+env(safe-area-inset-bottom))] shadow-[0_-10px_30px_rgba(34,28,22,0.05)]"
    >
      <div className="mx-auto flex w-full max-w-[860px] min-w-0 items-end gap-2">
        <label className="sr-only" htmlFor="research-question">
          質問
        </label>
        <textarea
          id="research-question"
          name="question"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder={
            activeRunId
              ? "回答を生成しています"
              : "市場・技術・企業動向について質問"
          }
          disabled={disabled}
          rows={2}
          maxLength={1000}
          className="min-h-16 min-w-0 flex-1 resize-none rounded-md border border-[var(--vector-line)] bg-[var(--vector-paper)] px-3 py-2 text-base leading-6 text-[var(--vector-ink)] outline-none transition focus:border-[var(--vector-accent)] focus:ring-2 focus:ring-[var(--vector-accent)]/20 disabled:cursor-not-allowed disabled:opacity-60"
        />
        {activeRunId ? (
          <Button
            type="button"
            variant="outline"
            className="h-16 border-[var(--vector-rule)] bg-[var(--vector-surface)] text-[var(--vector-ink)]"
            onClick={handleCancel}
            disabled={pending || isNavigationPending}
          >
            {pending ? (
              <Loader2 aria-hidden="true" className="animate-spin" />
            ) : (
              <Square aria-hidden="true" />
            )}
            停止
          </Button>
        ) : (
          <Button
            type="submit"
            className="h-16 bg-[var(--vector-accent)] text-[var(--vector-on-accent)] hover:bg-[var(--vector-accent-ink)]"
            disabled={disabled || !question.trim()}
          >
            {pending ? (
              <Loader2 aria-hidden="true" className="animate-spin" />
            ) : (
              <Send aria-hidden="true" />
            )}
            送信
          </Button>
        )}
      </div>
    </form>
  );
}
