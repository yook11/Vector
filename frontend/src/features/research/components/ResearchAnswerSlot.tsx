import { Bot } from "lucide-react";
import type { ReactNode } from "react";
import type { ResearchAssistantMessage } from "@/types/types.gen";
import { CitedAnswerContent } from "./CitedAnswerContent";

interface ResearchAnswerSlotProps {
  finalAnswer: ResearchAssistantMessage | null;
  children?: ReactNode;
}

export function ResearchAnswerSlot({
  finalAnswer,
  children,
}: ResearchAnswerSlotProps) {
  const isFinal = finalAnswer !== null;
  return (
    <article
      data-testid="research-answer-slot"
      data-research-answer-anchor="true"
      aria-busy={isFinal ? undefined : "true"}
      className="flex min-w-0 gap-3"
    >
      <div className="mt-1 flex size-8 shrink-0 items-center justify-center rounded-md bg-[var(--vector-accent-tint)] text-[var(--vector-accent-ink)]">
        <Bot aria-hidden="true" className="size-4" />
      </div>
      <div className="min-w-0 flex-1">
        {finalAnswer === null ? (
          children
        ) : (
          <>
            <div className="whitespace-pre-wrap break-words text-sm leading-7 text-[var(--vector-ink)] [overflow-wrap:anywhere]">
              <CitedAnswerContent
                content={finalAnswer.content}
                sources={finalAnswer.sources}
              />
            </div>
            {finalAnswer.missingAspects.length > 0 ? (
              <div className="mt-3 rounded-md border border-[var(--vector-rule)] bg-[var(--vector-paper)] px-3 py-2 text-xs text-[var(--vector-ink-muted)] break-words [overflow-wrap:anywhere]">
                {finalAnswer.missingAspects.join(" / ")}
              </div>
            ) : null}
          </>
        )}
      </div>
    </article>
  );
}
