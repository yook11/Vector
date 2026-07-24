"use client";

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useLayoutEffect,
  useMemo,
  useState,
} from "react";
import type {
  PaginatedResearchThreadResponse,
  ResearchThreadDetail,
} from "@/types/types.gen";
import { ResearchOperationProvider } from "./ResearchOperationBoundary";
import { ResearchSubmissionProvider } from "./ResearchSubmissionBoundary";
import { ResearchWorkspace } from "./ResearchWorkspace";

type ResearchRouteModel = {
  threads: PaginatedResearchThreadResponse;
  thread: ResearchThreadDetail | null;
  limit: number;
};

type ResearchRouteOutcome =
  | { kind: "initial" }
  | { kind: "committed"; model: ResearchRouteModel }
  | { kind: "rejected" };

interface ResearchRouteOutcomeContextValue {
  commit: (model: ResearchRouteModel) => void;
  reject: () => void;
}

const ResearchRouteOutcomeContext =
  createContext<ResearchRouteOutcomeContextValue | null>(null);

export function ResearchRouteHost({
  children,
  initialFallback,
}: {
  children: ReactNode;
  initialFallback: ReactNode;
}) {
  const [outcome, setOutcome] = useState<ResearchRouteOutcome>({
    kind: "initial",
  });
  const commit = useCallback((model: ResearchRouteModel) => {
    setOutcome({ kind: "committed", model });
  }, []);
  const reject = useCallback(() => {
    setOutcome({ kind: "rejected" });
  }, []);
  const contextValue = useMemo(() => ({ commit, reject }), [commit, reject]);

  return (
    <ResearchOperationProvider>
      <ResearchSubmissionProvider>
        <ResearchRouteOutcomeContext.Provider value={contextValue}>
          <div className="contents" data-research-route-host>
            {outcome.kind === "initial" ? (
              <div className="contents" data-research-route-initial>
                {initialFallback}
              </div>
            ) : null}
            {outcome.kind === "committed" ? (
              <div className="contents" data-research-route-retained>
                <ResearchWorkspace {...outcome.model} />
              </div>
            ) : null}
            <div className="contents" data-research-route-outlet>
              {children}
            </div>
          </div>
        </ResearchRouteOutcomeContext.Provider>
      </ResearchSubmissionProvider>
    </ResearchOperationProvider>
  );
}

function useResearchRouteOutcome(): ResearchRouteOutcomeContextValue {
  const value = useContext(ResearchRouteOutcomeContext);
  if (value === null) {
    throw new Error(
      "Research route outcome reporters must be used within ResearchRouteHost",
    );
  }
  return value;
}

export function ResearchRouteModelCommit({
  limit,
  thread,
  threads,
}: ResearchRouteModel) {
  const { commit } = useResearchRouteOutcome();

  useLayoutEffect(() => {
    commit({ limit, thread, threads });
  }, [commit, limit, thread, threads]);

  return null;
}

export function ResearchRouteRejectedOutcome() {
  const { reject } = useResearchRouteOutcome();

  useLayoutEffect(() => {
    reject();
  }, [reject]);

  return <span data-research-route-rejected hidden />;
}
