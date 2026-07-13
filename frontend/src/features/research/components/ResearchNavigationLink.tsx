"use client";

import { Loader2, MessageSquareText, Plus } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils/cn";
import {
  type ResearchNavigationTarget,
  useResearchNavigation,
} from "./ResearchNavigationBoundary";

type ThreadNavigationLinkProps = {
  variant: "thread";
  target: Extract<ResearchNavigationTarget, { kind: "thread" }>;
  active: boolean;
  title: string;
  idleMetaLabel: string;
  hasActiveRun: boolean;
};

type NewNavigationLinkProps = {
  variant: "new";
  target: Extract<ResearchNavigationTarget, { kind: "new" }>;
};

type MoreNavigationLinkProps = {
  variant: "more";
  target: Extract<ResearchNavigationTarget, { kind: "more" }>;
};

type ResearchNavigationLinkProps =
  | ThreadNavigationLinkProps
  | NewNavigationLinkProps
  | MoreNavigationLinkProps;

function sameTarget(
  current: ResearchNavigationTarget | null,
  target: ResearchNavigationTarget,
): boolean {
  return current?.kind === target.kind && current.href === target.href;
}

export function ResearchNavigationLink(props: ResearchNavigationLinkProps) {
  const pathname = usePathname();
  const {
    isNavigationPending,
    pendingTarget,
    navigate,
    dismissHistoryAfterSelection,
  } = useResearchNavigation();
  const target = props.target;
  const targetPending = sameTarget(pendingTarget, target);
  const active = props.variant === "thread" && props.active;

  function handleNavigate(event: { preventDefault: () => void }) {
    if (isNavigationPending) {
      event.preventDefault();
      return;
    }
    if (active || (props.target.kind === "new" && pathname === "/research")) {
      event.preventDefault();
      dismissHistoryAfterSelection(target);
      return;
    }
    event.preventDefault();
    if (navigate(target)) dismissHistoryAfterSelection(target);
  }

  const navigationAria = {
    "aria-busy": targetPending || undefined,
    "aria-disabled": isNavigationPending || undefined,
  } as const;

  if (props.variant === "new") {
    return (
      <Link
        href={props.target.href}
        onNavigate={handleNavigate}
        aria-label="新しいスレッド"
        title="新しいスレッド"
        className={cn(
          buttonVariants({ variant: "outline", size: "icon-sm" }),
          "border-[var(--vector-rule)] bg-[var(--vector-paper)]",
          isNavigationPending &&
            !targetPending &&
            "cursor-not-allowed opacity-45",
          targetPending &&
            "border-[var(--vector-accent)] bg-[var(--vector-accent-tint)] text-[var(--vector-accent-ink)] ring-1 ring-[var(--vector-accent)]/40",
        )}
        {...navigationAria}
      >
        {targetPending ? (
          <Loader2
            aria-hidden="true"
            className="animate-spin motion-reduce:animate-none"
          />
        ) : (
          <Plus aria-hidden="true" />
        )}
      </Link>
    );
  }

  if (props.variant === "more") {
    return (
      <Link
        href={target.href}
        onNavigate={handleNavigate}
        className={cn(
          buttonVariants({ variant: "outline" }),
          "w-full border-[var(--vector-rule)] bg-[var(--vector-paper)]",
          isNavigationPending &&
            !targetPending &&
            "cursor-not-allowed opacity-45",
          targetPending &&
            "border-[var(--vector-accent)] bg-[var(--vector-accent-tint)] text-[var(--vector-accent-ink)] ring-1 ring-[var(--vector-accent)]/40",
        )}
        {...navigationAria}
      >
        <Loader2
          data-icon="inline-start"
          aria-hidden="true"
          className={cn(
            "opacity-0 motion-reduce:animate-none",
            targetPending && "animate-spin opacity-100",
          )}
        />
        {targetPending ? "読み込み中…" : "さらに表示"}
      </Link>
    );
  }

  return (
    <Link
      href={target.href}
      onNavigate={handleNavigate}
      aria-current={props.active ? "page" : undefined}
      className={cn(
        "group flex items-start gap-2 rounded-md border border-transparent px-3 py-2.5 text-sm transition motion-reduce:transition-none",
        props.active
          ? "border-[var(--vector-accent)]/35 bg-[var(--vector-accent-tint)] text-[var(--vector-ink)]"
          : "text-[var(--vector-ink-soft)] hover:border-[var(--vector-line)] hover:bg-[var(--vector-paper)]",
        isNavigationPending &&
          !targetPending &&
          "cursor-not-allowed opacity-45",
        targetPending &&
          "border-[var(--vector-accent)] bg-[var(--vector-accent-tint)] text-[var(--vector-ink)] opacity-100 ring-1 ring-[var(--vector-accent)]/50",
      )}
      {...navigationAria}
    >
      <MessageSquareText
        aria-hidden="true"
        className="mt-0.5 size-4 shrink-0 text-[var(--vector-accent-ink)]"
      />
      <span className="min-w-0 flex-1">
        <span className="line-clamp-2 block font-medium leading-5">
          {props.title}
        </span>
        <span className="mt-1 block text-[11px] text-[var(--vector-ink-muted)]">
          {targetPending ? "読み込み中…" : props.idleMetaLabel}
        </span>
      </span>
      <span className="mt-0.5 flex size-3.5 shrink-0 items-center justify-center">
        {targetPending ? (
          <Loader2
            aria-hidden="true"
            className="animate-spin motion-reduce:animate-none"
          />
        ) : props.hasActiveRun ? (
          <Loader2 aria-label="実行中" className="animate-spin" />
        ) : null}
      </span>
    </Link>
  );
}
