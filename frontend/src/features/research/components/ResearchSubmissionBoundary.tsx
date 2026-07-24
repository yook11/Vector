"use client";

import { useRouter } from "next/navigation";
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import { useResearchOperation } from "./ResearchOperationBoundary";

type SubmittedRunTarget = { threadId: string; runId: string };
type ResearchCommittedModel = {
  threadId: string | null;
  committedRunIds: readonly string[];
};
type SubmissionPhase =
  | { kind: "awaiting-result" }
  | { kind: "awaiting-model"; target: SubmittedRunTarget };

export interface ResearchSubmissionContextValue {
  isSubmissionPending: boolean;
  beginSubmission: () => boolean;
  acceptSubmission: (target: SubmittedRunTarget, navigateHref?: string) => void;
  finishSubmission: () => void;
  reportModelCommit: (model: ResearchCommittedModel) => void;
}

const ResearchSubmissionContext =
  createContext<ResearchSubmissionContextValue | null>(null);

function matchesSubmittedRun(
  model: ResearchCommittedModel,
  target: SubmittedRunTarget,
) {
  return (
    model.threadId === target.threadId &&
    model.committedRunIds.includes(target.runId)
  );
}

function currentBrowserHref(): string | null {
  if (typeof window === "undefined") return null;
  return `${window.location.pathname}${window.location.search}`;
}

export function ResearchSubmissionProvider({
  children,
}: {
  children: ReactNode;
}) {
  const [phase, setPhase] = useState<SubmissionPhase | null>(null);
  const [pendingNavigationHref, setPendingNavigationHref] = useState<
    string | null
  >(null);
  const phaseRef = useRef<SubmissionPhase | null>(null);
  const submissionOriginHrefRef = useRef<string | null>(null);
  const dispatchedNavigationHrefRef = useRef<string | null>(null);
  const latestModelRef = useRef<ResearchCommittedModel>({
    threadId: null,
    committedRunIds: [],
  });
  const router = useRouter();
  const { claimOperation, ownsOperation, releaseOperation } =
    useResearchOperation();

  const settle = useCallback(() => {
    phaseRef.current = null;
    submissionOriginHrefRef.current = null;
    dispatchedNavigationHrefRef.current = null;
    setPhase(null);
    setPendingNavigationHref(null);
    releaseOperation("submission");
  }, [releaseOperation]);

  const beginSubmission = useCallback(() => {
    if (!claimOperation("submission")) return false;
    const nextPhase = { kind: "awaiting-result" } as const;
    phaseRef.current = nextPhase;
    submissionOriginHrefRef.current = currentBrowserHref();
    setPhase(nextPhase);
    return true;
  }, [claimOperation]);

  const acceptSubmission = useCallback(
    (target: SubmittedRunTarget, navigateHref?: string) => {
      if (!ownsOperation("submission")) return;
      if (navigateHref !== undefined) {
        dispatchedNavigationHrefRef.current = null;
        setPendingNavigationHref(navigateHref);
      }
      if (matchesSubmittedRun(latestModelRef.current, target)) {
        settle();
        return;
      }
      const nextPhase = { kind: "awaiting-model", target } as const;
      phaseRef.current = nextPhase;
      setPhase(nextPhase);
    },
    [ownsOperation, settle],
  );

  const finishSubmission = settle;
  const reportModelCommit = useCallback(
    (model: ResearchCommittedModel) => {
      latestModelRef.current = model;
      const current = phaseRef.current;
      if (
        current?.kind === "awaiting-model" &&
        matchesSubmittedRun(model, current.target)
      ) {
        settle();
      }
    },
    [settle],
  );

  useEffect(() => {
    if (pendingNavigationHref === null) return;
    if (dispatchedNavigationHrefRef.current === pendingNavigationHref) return;
    const originHref = submissionOriginHrefRef.current;
    if (
      !ownsOperation("submission") ||
      (originHref !== null && currentBrowserHref() !== originHref)
    ) {
      settle();
      return;
    }
    dispatchedNavigationHrefRef.current = pendingNavigationHref;
    router.replace(pendingNavigationHref);
  }, [ownsOperation, pendingNavigationHref, router, settle]);

  useEffect(
    () => () => {
      releaseOperation("submission");
    },
    [releaseOperation],
  );

  return (
    <ResearchSubmissionContext.Provider
      value={{
        isSubmissionPending: phase !== null,
        beginSubmission,
        acceptSubmission,
        finishSubmission,
        reportModelCommit,
      }}
    >
      {children}
    </ResearchSubmissionContext.Provider>
  );
}

export function useResearchSubmission(): ResearchSubmissionContextValue {
  const value = useContext(ResearchSubmissionContext);
  if (value === null) {
    throw new Error(
      "useResearchSubmission must be used within ResearchSubmissionProvider",
    );
  }
  return value;
}
