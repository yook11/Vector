"use client";

import { Loader2, PanelLeftClose, PanelLeftOpen, X } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  createContext,
  type ReactNode,
  type Ref,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  useTransition,
} from "react";
import { PaperSurface } from "@/components/paper";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { ResearchLiveAnnouncementOwner } from "./ResearchLiveAnnouncer";
import { useResearchOperation } from "./ResearchOperationBoundary";

const DESKTOP_HISTORY_QUERY = "(min-width: 64rem)";

export type ResearchNavigationTarget =
  | {
      kind: "thread";
      href: string;
      threadId: string;
      label: string;
    }
  | {
      kind: "new";
      href: "/research";
      label: "新しいスレッド";
    }
  | {
      kind: "more";
      href: string;
      label: "さらに表示";
    };

interface ResearchNavigationContextValue {
  isNavigationPending: boolean;
  pendingTarget: ResearchNavigationTarget | null;
  navigate: (target: ResearchNavigationTarget) => boolean;
  dismissHistoryAfterSelection: (target: ResearchNavigationTarget) => void;
}

const ResearchNavigationContext =
  createContext<ResearchNavigationContextValue | null>(null);

function pendingStatus(target: ResearchNavigationTarget): string {
  switch (target.kind) {
    case "thread":
      return `「${target.label}」を読み込み中…`;
    case "new":
      return "新しいスレッドを準備中…";
    case "more":
      return "スレッド一覧を読み込み中…";
  }
}

function subscribeDesktopViewport(listener: () => void): () => void {
  if (typeof window.matchMedia !== "function") return () => undefined;
  const media = window.matchMedia(DESKTOP_HISTORY_QUERY);
  media.addEventListener("change", listener);
  return () => media.removeEventListener("change", listener);
}

function desktopViewportSnapshot(): boolean {
  if (typeof window.matchMedia !== "function") return true;
  return window.matchMedia(DESKTOP_HISTORY_QUERY).matches;
}

function useDesktopViewport(): boolean {
  return useSyncExternalStore(
    subscribeDesktopViewport,
    desktopViewportSnapshot,
    () => true,
  );
}

interface HistoryToggleButtonProps {
  expanded: boolean;
  onClick?: () => void;
  buttonRef?: Ref<HTMLButtonElement>;
}

function HistoryToggleButton({
  expanded,
  onClick,
  buttonRef,
}: HistoryToggleButtonProps) {
  const label = expanded ? "履歴を閉じる" : "履歴を開く";
  const Icon = expanded ? PanelLeftClose : PanelLeftOpen;
  return (
    <Button
      type="button"
      variant="outline"
      size="icon-lg"
      className="size-11 shadow-none"
      aria-label={label}
      title={label}
      aria-controls="research-history"
      aria-expanded={expanded}
      onClick={onClick}
      ref={buttonRef}
    >
      <Icon aria-hidden="true" />
    </Button>
  );
}

export function useResearchNavigation(): ResearchNavigationContextValue {
  const value = useContext(ResearchNavigationContext);
  if (value === null) {
    throw new Error(
      "useResearchNavigation must be used within ResearchNavigationBoundary",
    );
  }
  return value;
}

interface ResearchNavigationBoundaryProps {
  sidebar: ReactNode;
  children: ReactNode;
}

export function ResearchNavigationBoundary({
  sidebar,
  children,
}: ResearchNavigationBoundaryProps) {
  return (
    <ResearchNavigationBoundaryContent sidebar={sidebar}>
      {children}
    </ResearchNavigationBoundaryContent>
  );
}

function ResearchNavigationBoundaryContent({
  sidebar,
  children,
}: ResearchNavigationBoundaryProps) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const isDesktop = useDesktopViewport();
  const [, startTransition] = useTransition();
  const [pendingTarget, setPendingTarget] =
    useState<ResearchNavigationTarget | null>(null);
  const [desktopHistoryOpen, setDesktopHistoryOpen] = useState(true);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [researchAnnouncement, setResearchAnnouncement] = useState("");
  const { claimOperation, operation, releaseOperation } =
    useResearchOperation();
  const historyToggleRef = useRef<HTMLButtonElement>(null);
  const drawerCloseRef = useRef<HTMLButtonElement>(null);
  const query = searchParams.toString();
  const currentHref = query ? `${pathname}?${query}` : pathname;
  const pendingPathname =
    pendingTarget === null
      ? null
      : new URL(pendingTarget.href, "http://research.local").pathname;
  const routeCommitted =
    pendingTarget !== null &&
    (pendingTarget.kind === "more"
      ? currentHref === pendingTarget.href
      : pathname === pendingPathname);

  useEffect(() => {
    if (!routeCommitted) return;
    releaseOperation("navigation");
    setPendingTarget(null);
  }, [releaseOperation, routeCommitted]);

  useEffect(
    () => () => {
      releaseOperation("navigation");
    },
    [releaseOperation],
  );

  useEffect(() => {
    if (isDesktop) setDrawerOpen(false);
  }, [isDesktop]);

  // cache済み旧subtreeのusePathnameは更新されないため、実URL commitも監視する。
  useEffect(() => {
    if (pendingTarget === null) return;
    const target = pendingTarget;
    const targetPathname = new URL(target.href, window.location.origin)
      .pathname;
    let animationFrame = 0;

    function clearAfterBrowserCommit() {
      const browserHref = `${window.location.pathname}${window.location.search}`;
      const committed =
        target.kind === "more"
          ? browserHref === target.href
          : window.location.pathname === targetPathname;
      if (committed) {
        releaseOperation("navigation");
        setPendingTarget(null);
        return;
      }
      animationFrame = window.requestAnimationFrame(clearAfterBrowserCommit);
    }

    animationFrame = window.requestAnimationFrame(clearAfterBrowserCommit);
    return () => window.cancelAnimationFrame(animationFrame);
  }, [pendingTarget, releaseOperation]);

  const navigate = useCallback(
    (target: ResearchNavigationTarget) => {
      if (!claimOperation("navigation")) return false;
      setPendingTarget(target);
      try {
        startTransition(() => {
          router.push(target.href);
        });
      } catch (error) {
        setPendingTarget(null);
        releaseOperation("navigation");
        throw error;
      }
      return true;
    },
    [claimOperation, releaseOperation, router],
  );

  const dismissHistoryAfterSelection = useCallback(
    (target: ResearchNavigationTarget) => {
      if (target.kind !== "more") setDrawerOpen(false);
    },
    [],
  );

  const isNavigationPending = pendingTarget !== null;
  const isResearchOperationPending = operation !== null;
  const status = pendingTarget === null ? "" : pendingStatus(pendingTarget);
  const reportResearchAnnouncement = useCallback((announcement: string) => {
    setResearchAnnouncement((current) =>
      current === announcement ? current : announcement,
    );
  }, []);
  return (
    <ResearchNavigationContext.Provider
      value={{
        isNavigationPending,
        pendingTarget,
        navigate,
        dismissHistoryAfterSelection,
      }}
    >
      <ResearchLiveAnnouncementOwner report={reportResearchAnnouncement}>
        <main
          aria-busy={isResearchOperationPending}
          className="relative z-10 flex h-full min-h-0 w-full min-w-0 flex-col overflow-hidden border-x border-b border-[var(--vector-rule)] bg-[var(--vector-surface)] md:flex-row"
        >
          {isDesktop && desktopHistoryOpen ? (
            <div className="hidden h-full min-h-0 w-[320px] shrink-0 lg:flex">
              {sidebar}
            </div>
          ) : null}
          <div className="relative flex min-h-0 min-w-0 flex-1">
            <div className="absolute top-3 left-3 z-20">
              {isDesktop ? (
                <HistoryToggleButton
                  expanded={desktopHistoryOpen}
                  onClick={() => setDesktopHistoryOpen((open) => !open)}
                />
              ) : (
                <Sheet open={drawerOpen} onOpenChange={setDrawerOpen}>
                  <SheetTrigger asChild>
                    <HistoryToggleButton
                      expanded={drawerOpen}
                      buttonRef={historyToggleRef}
                    />
                  </SheetTrigger>
                  <SheetContent
                    side="left"
                    showCloseButton={false}
                    aria-modal="true"
                    className="h-dvh w-[min(88vw,320px)] max-w-none gap-0 p-0"
                    onOpenAutoFocus={(event) => {
                      event.preventDefault();
                      drawerCloseRef.current?.focus();
                    }}
                    onCloseAutoFocus={(event) => {
                      event.preventDefault();
                      historyToggleRef.current?.focus();
                    }}
                  >
                    <PaperSurface className="flex h-full min-h-0 flex-col overflow-hidden">
                      <SheetHeader className="shrink-0 flex-row items-center justify-between gap-3 border-b border-[var(--vector-rule)] px-4 py-3 text-left">
                        <div className="min-w-0">
                          <SheetTitle className="text-left text-base text-[var(--vector-ink)]">
                            リサーチ履歴
                          </SheetTitle>
                          <SheetDescription className="sr-only">
                            リサーチスレッドを選択する
                          </SheetDescription>
                        </div>
                        <SheetClose asChild>
                          <Button
                            ref={drawerCloseRef}
                            type="button"
                            variant="ghost"
                            size="icon-lg"
                            className="size-11"
                            aria-label="履歴を閉じる"
                          >
                            <X aria-hidden="true" />
                          </Button>
                        </SheetClose>
                      </SheetHeader>
                      <div className="min-h-0 flex-1">{sidebar}</div>
                    </PaperSurface>
                  </SheetContent>
                </Sheet>
              )}
            </div>
            {children}
            {pendingTarget !== null && (
              <div
                data-testid="research-navigation-overlay"
                aria-hidden="true"
                className="pointer-events-none absolute inset-0 flex items-start justify-end bg-[var(--vector-surface-2)]/40 p-4 motion-safe:animate-in motion-safe:fade-in motion-safe:duration-150"
              >
                <div className="flex max-w-full items-center gap-2 rounded-md border border-[var(--vector-rule)] bg-[var(--vector-surface)]/95 px-4 py-3 text-sm font-medium text-[var(--vector-ink)] shadow-sm">
                  <Loader2
                    aria-hidden="true"
                    className="shrink-0 animate-spin motion-reduce:animate-none"
                  />
                  <p className="min-w-0 truncate">{status}</p>
                </div>
              </div>
            )}
          </div>
          <div
            role="status"
            aria-live="polite"
            aria-atomic="true"
            className="sr-only"
          >
            {status || researchAnnouncement}
          </div>
        </main>
      </ResearchLiveAnnouncementOwner>
    </ResearchNavigationContext.Provider>
  );
}
