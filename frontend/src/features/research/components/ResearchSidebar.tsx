import { Loader2, MessageSquareText, Plus } from "lucide-react";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils/cn";
import type { PaginatedResearchThreadResponse } from "@/types/types.gen";
import {
  DEFAULT_RESEARCH_THREAD_LIMIT,
  nextResearchLimit,
} from "../schemas/research";

interface ResearchSidebarProps {
  threads: PaginatedResearchThreadResponse;
  activeThreadId?: string;
  limit: number;
}

function researchHref(threadId: string | undefined, limit: number): string {
  const path = threadId === undefined ? "/research" : `/research/${threadId}`;
  if (limit === DEFAULT_RESEARCH_THREAD_LIMIT) return path;
  return `${path}?limit=${limit}`;
}

export function ResearchSidebar({
  threads,
  activeThreadId,
  limit,
}: ResearchSidebarProps) {
  const nextLimit = nextResearchLimit(limit, threads.total);
  return (
    <aside className="flex min-h-0 flex-col border-b border-[var(--vector-rule)] bg-[var(--vector-surface)]/90 md:w-[320px] md:border-r md:border-b-0">
      <div className="flex items-center justify-between gap-3 border-b border-[var(--vector-rule)] px-4 py-3">
        <div>
          <p
            className="text-[11px] font-semibold uppercase text-[var(--vector-accent-ink)]"
            style={{ fontFamily: "var(--font-vector-display)" }}
          >
            RESEARCH
          </p>
          <h1 className="text-base font-semibold text-[var(--vector-ink)]">
            Agent threads
          </h1>
        </div>
        <Button
          asChild
          size="icon-sm"
          variant="outline"
          className="border-[var(--vector-rule)] bg-[var(--vector-paper)]"
        >
          <Link
            href="/research"
            aria-label="新しいスレッド"
            title="新しいスレッド"
          >
            <Plus aria-hidden="true" />
          </Link>
        </Button>
      </div>

      <nav className="min-h-0 flex-1 overflow-y-auto p-2">
        {threads.items.length === 0 ? (
          <div className="px-3 py-10 text-center text-sm text-[var(--vector-ink-muted)]">
            履歴はまだありません。
          </div>
        ) : (
          <div className="space-y-1">
            {threads.items.map((thread) => {
              const active = thread.threadId === activeThreadId;
              return (
                <Link
                  key={thread.threadId}
                  href={researchHref(thread.threadId, limit)}
                  className={cn(
                    "group flex items-start gap-2 rounded-md border border-transparent px-3 py-2.5 text-sm transition",
                    active
                      ? "border-[var(--vector-accent)]/35 bg-[var(--vector-accent-tint)] text-[var(--vector-ink)]"
                      : "text-[var(--vector-ink-soft)] hover:border-[var(--vector-line)] hover:bg-[var(--vector-paper)]",
                  )}
                >
                  <MessageSquareText
                    aria-hidden="true"
                    className="mt-0.5 size-4 shrink-0 text-[var(--vector-accent-ink)]"
                  />
                  <span className="min-w-0 flex-1">
                    <span className="line-clamp-2 block font-medium leading-5">
                      {thread.title}
                    </span>
                    <span className="mt-1 block text-[11px] text-[var(--vector-ink-muted)]">
                      {new Intl.DateTimeFormat("ja-JP", {
                        month: "short",
                        day: "numeric",
                        hour: "2-digit",
                        minute: "2-digit",
                        timeZone: "Asia/Tokyo",
                      }).format(new Date(thread.updatedAt))}
                    </span>
                  </span>
                  {thread.hasActiveRun && (
                    <Loader2
                      aria-label="実行中"
                      className="mt-0.5 size-3.5 shrink-0 animate-spin text-[var(--vector-accent)]"
                    />
                  )}
                </Link>
              );
            })}
          </div>
        )}
      </nav>

      {nextLimit !== null && (
        <div className="border-t border-[var(--vector-rule)] p-3">
          <Button
            asChild
            variant="outline"
            className="w-full border-[var(--vector-rule)] bg-[var(--vector-paper)]"
          >
            <Link href={researchHref(activeThreadId, nextLimit)}>
              さらに表示
            </Link>
          </Button>
        </div>
      )}
    </aside>
  );
}
