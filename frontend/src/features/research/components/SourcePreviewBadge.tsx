"use client";

import { ExternalLink, FileText } from "lucide-react";
import type { PointerEvent } from "react";
import { useEffect, useRef, useState } from "react";
import { PendingAwareLink } from "@/components/layout/PageNavigation";
import { Badge } from "@/components/ui/badge";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { formatDate } from "@/lib/date";
import type { ResearchAssistantMessage } from "@/types/types.gen";

type ResearchCitationSource = ResearchAssistantMessage["sources"][number];

interface SourcePreviewBadgeProps {
  source: ResearchCitationSource;
}

function sourceKindLabel(source: ResearchCitationSource): string {
  return source.kind === "external_url" ? "外部" : "内部記事";
}

function publishedText(source: ResearchCitationSource): string | null {
  return source.publishedAt ? formatDate(source.publishedAt) : null;
}

function SourceTitle({ source }: { source: ResearchCitationSource }) {
  if (source.kind === "external_url") {
    return (
      <a
        href={source.url}
        target="_blank"
        rel="noreferrer"
        className="inline-flex max-w-full min-w-0 items-center gap-1 text-sm font-semibold text-popover-foreground underline-offset-4 hover:underline"
      >
        <span className="min-w-0 line-clamp-2">{source.title}</span>
        <ExternalLink aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
      </a>
    );
  }

  if (source.articleId !== null) {
    return (
      <PendingAwareLink
        href={`/news/${source.articleId}`}
        className="inline-flex max-w-full min-w-0 items-center gap-1 text-sm font-semibold text-popover-foreground underline-offset-4 hover:underline"
      >
        <span className="min-w-0 line-clamp-2">{source.title}</span>
        <FileText aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
      </PendingAwareLink>
    );
  }

  return (
    <p className="line-clamp-2 max-w-full text-sm font-semibold text-popover-foreground">
      {source.title}
    </p>
  );
}

function SourceMeta({ source }: { source: ResearchCitationSource }) {
  const dateText = publishedText(source);
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] leading-4 text-muted-foreground">
      {source.kind === "external_url" && source.sourceName ? (
        <span className="min-w-0 max-w-[12rem] truncate">
          {source.sourceName}
        </span>
      ) : null}
      {dateText ? (
        <time dateTime={source.publishedAt ?? ""}>{dateText}</time>
      ) : null}
    </div>
  );
}

export function SourcePreviewBadge({ source }: SourcePreviewBadgeProps) {
  const [open, setOpen] = useState(false);
  const hoverTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  function clearHoverTimer() {
    if (hoverTimer.current !== null) {
      clearTimeout(hoverTimer.current);
      hoverTimer.current = null;
    }
  }

  function clearCloseTimer() {
    if (closeTimer.current !== null) {
      clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
  }

  function openPreview() {
    clearHoverTimer();
    clearCloseTimer();
    setOpen(true);
  }

  function scheduleHoverOpen(event: PointerEvent<HTMLButtonElement>) {
    if (event.pointerType === "touch") return;
    clearHoverTimer();
    clearCloseTimer();
    hoverTimer.current = setTimeout(() => setOpen(true), 120);
  }

  function scheduleTriggerClose(event: PointerEvent<HTMLButtonElement>) {
    if (event.pointerType === "touch") return;
    scheduleClose();
  }

  function scheduleClose() {
    clearHoverTimer();
    clearCloseTimer();
    closeTimer.current = setTimeout(() => setOpen(false), 160);
  }

  useEffect(() => {
    return () => {
      if (hoverTimer.current !== null) {
        clearTimeout(hoverTimer.current);
      }
      if (closeTimer.current !== null) {
        clearTimeout(closeTimer.current);
      }
    };
  }, []);

  return (
    <Popover
      open={open}
      onOpenChange={(nextOpen) => {
        clearHoverTimer();
        clearCloseTimer();
        setOpen(nextOpen);
      }}
    >
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={`出典 ${source.sourceRef}`}
          onPointerEnter={scheduleHoverOpen}
          onPointerLeave={scheduleTriggerClose}
          onPointerDown={clearHoverTimer}
          className="mx-0.5 inline-flex h-[1.25em] min-w-[1.25em] translate-y-[-0.32em] items-center justify-center rounded-[4px] bg-[var(--vector-accent)] px-1 text-[0.62em] font-bold leading-none text-[var(--vector-on-accent)] align-baseline shadow-[0_0_0_1px_color-mix(in_oklab,var(--vector-accent)_65%,var(--vector-paper))] transition hover:bg-[var(--vector-accent-ink)] hover:text-[var(--vector-on-accent)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--vector-accent)]/35"
        >
          {source.sourceRef}
        </button>
      </PopoverTrigger>
      <PopoverContent
        side="top"
        align="center"
        sideOffset={8}
        className="flex w-[min(280px,calc(100vw-2rem))] min-w-0 flex-col gap-2 overflow-hidden rounded-md border-border bg-popover p-3 text-[12px] leading-5 text-popover-foreground shadow-[0_18px_48px_-24px_rgba(0,0,0,0.95)]"
        style={{ backgroundColor: "var(--popover, #171717)" }}
        onPointerEnter={openPreview}
        onPointerLeave={scheduleClose}
      >
        <div className="flex min-w-0 items-center gap-1.5">
          <Badge
            variant="default"
            className="h-5 rounded-sm px-1.5 text-[10px]"
          >
            {sourceKindLabel(source)}
          </Badge>
          <span className="text-[11px] font-semibold text-primary">
            出典 {source.sourceRef}
          </span>
        </div>
        <div className="flex min-w-0 flex-col gap-1">
          <SourceTitle source={source} />
          <SourceMeta source={source} />
        </div>
        {source.kind === "external_url" ? (
          <p className="line-clamp-3 break-words border-t border-border pt-2 text-[11px] leading-[1.55] text-muted-foreground [overflow-wrap:anywhere]">
            {source.evidenceClaim}
          </p>
        ) : null}
      </PopoverContent>
    </Popover>
  );
}
