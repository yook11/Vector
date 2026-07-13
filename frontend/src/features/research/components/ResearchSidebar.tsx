import type { PaginatedResearchThreadResponse } from "@/types/types.gen";
import {
  DEFAULT_RESEARCH_THREAD_LIMIT,
  nextResearchLimit,
} from "../schemas/research";
import { ResearchNavigationLink } from "./ResearchNavigationLink";

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
    <aside
      id="research-history"
      className="flex h-full min-h-0 w-full flex-col border-r border-[var(--vector-rule)] bg-[var(--vector-surface)]/90"
    >
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
        <ResearchNavigationLink
          variant="new"
          target={{
            kind: "new",
            href: "/research",
            label: "新しいスレッド",
          }}
        />
      </div>

      <nav
        aria-label="リサーチ履歴"
        className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-2"
      >
        {threads.items.length === 0 ? (
          <div className="px-3 py-10 text-center text-sm text-[var(--vector-ink-muted)]">
            履歴はまだありません。
          </div>
        ) : (
          <div className="flex flex-col gap-1">
            {threads.items.map((thread) => {
              const active = thread.threadId === activeThreadId;
              return (
                <ResearchNavigationLink
                  key={thread.threadId}
                  variant="thread"
                  target={{
                    kind: "thread",
                    href: researchHref(thread.threadId, limit),
                    threadId: thread.threadId,
                    label: thread.title,
                  }}
                  active={active}
                  title={thread.title}
                  idleMetaLabel={new Intl.DateTimeFormat("ja-JP", {
                    month: "short",
                    day: "numeric",
                    hour: "2-digit",
                    minute: "2-digit",
                    timeZone: "Asia/Tokyo",
                  }).format(new Date(thread.updatedAt))}
                  hasActiveRun={thread.hasActiveRun}
                />
              );
            })}
          </div>
        )}
      </nav>

      {nextLimit !== null && (
        <div className="border-t border-[var(--vector-rule)] p-3">
          <ResearchNavigationLink
            variant="more"
            target={{
              kind: "more",
              href: researchHref(activeThreadId, nextLimit),
              label: "さらに表示",
            }}
          />
        </div>
      )}
    </aside>
  );
}
