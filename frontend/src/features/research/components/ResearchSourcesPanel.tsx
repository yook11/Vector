"use client";

import { ExternalLink, FileText, Library, X } from "lucide-react";
import Link from "next/link";
import {
  type ReactNode,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { formatDate } from "@/lib/date";
import type {
  ResearchAssistantMessage,
  ResearchThreadDetail,
} from "@/types/types.gen";
import { useResearchNavigation } from "./ResearchNavigationBoundary";

const WIDE_SOURCES_QUERY = "(min-width: 80rem)";
const INLINE_SOURCES_ID = "research-sources-inline";
const SHEET_SOURCES_ID = "research-sources-sheet";

type ResearchSource = ResearchAssistantMessage["sources"][number];
type SourcesSurface = "closed" | "inline" | "sheet";

interface SourcesDisclosureState {
  threadId: string;
  surface: SourcesSurface;
}

interface PendingAnswerScrollRestore {
  owner: HTMLElement;
  scrollTop: number;
  surface: SourcesSurface;
}

function subscribeWideSources(listener: () => void): () => void {
  if (typeof window.matchMedia !== "function") return () => undefined;
  const media = window.matchMedia(WIDE_SOURCES_QUERY);
  media.addEventListener("change", listener);
  return () => media.removeEventListener("change", listener);
}

function wideSourcesSnapshot(): boolean {
  if (typeof window.matchMedia !== "function") return true;
  return window.matchMedia(WIDE_SOURCES_QUERY).matches;
}

function useWideSourcesViewport(): boolean {
  return useSyncExternalStore(
    subscribeWideSources,
    wideSourcesSnapshot,
    () => true,
  );
}

function SourceTitle({ source }: { source: ResearchSource }) {
  const className =
    "min-w-0 break-words text-xs font-semibold leading-5 text-[var(--vector-ink)] underline-offset-4 [overflow-wrap:anywhere] hover:underline";
  if (source.kind === "external_url") {
    return (
      <a
        href={source.url}
        target="_blank"
        rel="noreferrer"
        className={className}
      >
        {source.title}
      </a>
    );
  }
  if (source.articleId !== null) {
    return (
      <Link href={`/news/${source.articleId}`} className={className}>
        {source.title}
      </Link>
    );
  }
  return <p className={className}>{source.title}</p>;
}

function SourceCard({ source }: { source: ResearchSource }) {
  const published = source.publishedAt ? formatDate(source.publishedAt) : null;
  return (
    <article className="min-w-0 border-b border-[var(--vector-rule)] py-3 last:border-b-0">
      <div className="flex min-w-0 items-start gap-2.5">
        <span className="inline-flex min-h-5 min-w-5 shrink-0 items-center justify-center rounded-sm bg-[var(--vector-accent-tint)] px-1 text-[11px] font-semibold text-[var(--vector-accent-ink)]">
          {source.sourceRef}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-start gap-1.5">
            <SourceTitle source={source} />
            {source.kind === "external_url" ? (
              <ExternalLink
                aria-hidden="true"
                className="mt-1 size-3 shrink-0 text-[var(--vector-ink-muted)]"
              />
            ) : source.articleId !== null ? (
              <FileText
                aria-hidden="true"
                className="mt-1 size-3 shrink-0 text-[var(--vector-ink-muted)]"
              />
            ) : null}
          </div>
          <div className="mt-1 flex min-w-0 flex-wrap gap-x-2 gap-y-1 text-[11px] text-[var(--vector-ink-muted)]">
            {source.kind === "external_url" && source.sourceName ? (
              <span className="min-w-0 break-words [overflow-wrap:anywhere]">
                {source.sourceName}
              </span>
            ) : null}
            {published ? (
              <time dateTime={source.publishedAt ?? undefined}>
                {published}
              </time>
            ) : null}
          </div>
          {source.kind === "external_url" ? (
            <p className="mt-2 line-clamp-3 min-w-0 break-words text-[11px] leading-4 text-[var(--vector-ink-soft)] [overflow-wrap:anywhere]">
              {source.evidenceClaim}
            </p>
          ) : null}
        </div>
      </div>
    </article>
  );
}

function SourcesList({
  messages,
}: {
  messages: ResearchThreadDetail["messages"];
}) {
  const groups = messages.flatMap((message) =>
    message.role === "assistant" && message.sources.length > 0 ? [message] : [],
  );

  if (groups.length === 0) {
    return (
      <div className="flex min-h-full items-center justify-center px-4 py-12 text-center text-sm text-[var(--vector-ink-muted)]">
        表示できるソースはありません
      </div>
    );
  }

  return (
    <div className="flex min-w-0 flex-col gap-6 p-3">
      {groups.map((message, index) => (
        <section
          key={message.seq}
          aria-labelledby={`research-source-group-${message.seq}`}
          className="min-w-0"
        >
          <h3
            id={`research-source-group-${message.seq}`}
            className="mb-2 text-[11px] font-semibold tracking-[0.08em] text-[var(--vector-accent-ink)]"
          >
            回答 {index + 1}
          </h3>
          <div className="flex min-w-0 flex-col">
            {message.sources.map((source) => (
              <SourceCard
                key={`${message.seq}-${source.sourceRef}`}
                source={source}
              />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

interface ResearchSourcesPanelProps {
  threadId: string;
  messages: ResearchThreadDetail["messages"];
  headerLeading: ReactNode;
  headerActions: ReactNode;
  answerPanel: ReactNode;
  composer: ReactNode;
}

export function ResearchSourcesPanel({
  threadId,
  messages,
  headerLeading,
  headerActions,
  answerPanel,
  composer,
}: ResearchSourcesPanelProps) {
  const isWide = useWideSourcesViewport();
  const { pendingTarget } = useResearchNavigation();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const sheetCloseRef = useRef<HTMLButtonElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const disconnectedRef = useRef(false);
  const pendingAnswerScrollRestoreRef =
    useRef<PendingAnswerScrollRestore | null>(null);
  const sourceCount = messages.reduce(
    (count, message) =>
      count + (message.role === "assistant" ? message.sources.length : 0),
    0,
  );
  const hasSources = sourceCount > 0;
  const [disclosure, setDisclosure] = useState<SourcesDisclosureState>(() => ({
    threadId,
    surface: "closed",
  }));
  const navigatingAway =
    pendingTarget?.kind === "new" ||
    (pendingTarget?.kind === "thread" && pendingTarget.threadId !== threadId);
  const selectedSurface =
    disclosure.threadId === threadId &&
    !navigatingAway &&
    !disconnectedRef.current
      ? disclosure.surface
      : "closed";
  const surface =
    hasSources &&
    ((selectedSurface === "inline" && isWide) ||
      (selectedSurface === "sheet" && !isWide))
      ? selectedSurface
      : "closed";
  const controlledId =
    surface === "inline"
      ? INLINE_SOURCES_ID
      : surface === "sheet"
        ? SHEET_SOURCES_ID
        : undefined;

  useLayoutEffect(() => {
    if (disconnectedRef.current) {
      disconnectedRef.current = false;
      setDisclosure({ threadId, surface: "closed" });
    }
    return () => {
      disconnectedRef.current = true;
    };
  }, [threadId]);

  useEffect(() => {
    if (disclosure.threadId === threadId && disclosure.surface === surface) {
      return;
    }
    setDisclosure({ threadId, surface: "closed" });
  }, [disclosure.surface, disclosure.threadId, surface, threadId]);

  useLayoutEffect(() => {
    const pending = pendingAnswerScrollRestoreRef.current;
    if (pending === null || pending.surface !== surface) return;
    pendingAnswerScrollRestoreRef.current = null;
    const currentOwner = contentRef.current?.querySelector<HTMLElement>(
      "[data-research-answer-scroll-region]",
    );
    if (currentOwner === pending.owner) {
      currentOwner.scrollTop = pending.scrollTop;
    }
  }, [surface]);

  function captureAnswerScroll(nextSurface: SourcesSurface) {
    const owner = contentRef.current?.querySelector<HTMLElement>(
      "[data-research-answer-scroll-region]",
    );
    if (owner === undefined || owner === null) return;
    pendingAnswerScrollRestoreRef.current = {
      owner,
      scrollTop: owner.scrollTop,
      surface: nextSurface,
    };
  }

  function toggleSources() {
    if (!hasSources) return;
    if (isWide) {
      const nextSurface = surface === "inline" ? "closed" : "inline";
      captureAnswerScroll(nextSurface);
      setDisclosure({
        threadId,
        surface: nextSurface,
      });
      return;
    }
    setDisclosure({ threadId, surface: "sheet" });
  }

  function closeSources() {
    setDisclosure({ threadId, surface: "closed" });
  }

  return (
    <>
      <header className="flex shrink-0 items-center justify-between gap-3 border-b border-[var(--vector-rule)] bg-[var(--vector-surface)]/92 py-3 pr-4 pl-16">
        {headerLeading}
        <div className="flex shrink-0 items-center gap-2">
          <Button
            ref={triggerRef}
            type="button"
            variant="outline"
            size="sm"
            aria-expanded={surface !== "closed"}
            aria-controls={controlledId}
            disabled={!hasSources}
            onClick={toggleSources}
            className="border-[var(--vector-rule)] bg-[var(--vector-paper)] shadow-none"
          >
            <Library aria-hidden="true" className="size-3.5" />
            <span>ソース</span>
            <span className="rounded-sm bg-[var(--vector-accent-tint)] px-1.5 text-[11px] text-[var(--vector-accent-ink)]">
              {sourceCount}
            </span>
          </Button>
          {headerActions}
        </div>
      </header>

      <div ref={contentRef} className="flex min-h-0 min-w-0 flex-1">
        {answerPanel}
        {surface === "inline" ? (
          <aside
            id={INLINE_SOURCES_ID}
            aria-label="ソース"
            className="flex h-full min-h-0 w-80 shrink-0 flex-col overflow-hidden border-l border-[var(--vector-rule)] bg-[var(--vector-paper)]"
          >
            <div className="flex shrink-0 items-center justify-between border-b border-[var(--vector-rule)] px-3 py-2.5">
              <h2 className="text-sm font-semibold text-[var(--vector-ink)]">
                ソース
              </h2>
              <span className="text-xs text-[var(--vector-ink-muted)]">
                {sourceCount}件
              </span>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
              <SourcesList messages={messages} />
            </div>
          </aside>
        ) : null}
      </div>

      <Sheet
        open={surface === "sheet"}
        onOpenChange={(open) => {
          if (!open) closeSources();
        }}
      >
        <SheetContent
          id={SHEET_SOURCES_ID}
          side="right"
          showCloseButton={false}
          aria-modal="true"
          className="right-0 h-dvh w-[min(92vw,360px)] max-w-none gap-0 border-l p-0"
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            sheetCloseRef.current?.focus();
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            triggerRef.current?.focus();
          }}
        >
          <SheetHeader className="shrink-0 flex-row items-center justify-between gap-3 border-b border-[var(--vector-rule)] px-4 py-3 text-left">
            <div className="min-w-0">
              <SheetTitle className="text-left text-base text-[var(--vector-ink)]">
                ソース
              </SheetTitle>
              <SheetDescription className="text-left">
                確定済み回答のソース {sourceCount}件
              </SheetDescription>
            </div>
            <SheetClose asChild>
              <Button
                ref={sheetCloseRef}
                type="button"
                variant="ghost"
                size="icon-lg"
                className="size-11"
                aria-label="ソースを閉じる"
              >
                <X aria-hidden="true" />
              </Button>
            </SheetClose>
          </SheetHeader>
          <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
            <SourcesList messages={messages} />
          </div>
        </SheetContent>
      </Sheet>

      <div className="flex min-w-0 shrink-0">
        <div className="min-w-0 flex-1">{composer}</div>
        {surface === "inline" ? (
          <div
            aria-hidden="true"
            className="w-80 shrink-0 border-t border-l border-[var(--vector-rule)] bg-[var(--vector-paper)]"
          />
        ) : null}
      </div>
    </>
  );
}
