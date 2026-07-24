"use client";

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
} from "react";

export type ResearchOperationKind = "delete" | "navigation" | "submission";

interface ResearchOperationContextValue {
  operation: ResearchOperationKind | null;
  claimOperation: (kind: ResearchOperationKind) => boolean;
  ownsOperation: (kind: ResearchOperationKind) => boolean;
  releaseOperation: (kind: ResearchOperationKind) => void;
}

const ResearchOperationContext =
  createContext<ResearchOperationContextValue | null>(null);

export function ResearchOperationProvider({
  children,
}: {
  children: ReactNode;
}) {
  const operationRef = useRef<ResearchOperationKind | null>(null);
  const [operation, setOperation] = useState<ResearchOperationKind | null>(
    null,
  );

  const claimOperation = useCallback((kind: ResearchOperationKind) => {
    if (operationRef.current !== null) return false;
    operationRef.current = kind;
    setOperation(kind);
    return true;
  }, []);

  const ownsOperation = useCallback(
    (kind: ResearchOperationKind) => operationRef.current === kind,
    [],
  );

  const releaseOperation = useCallback((kind: ResearchOperationKind) => {
    if (operationRef.current !== kind) return;
    operationRef.current = null;
    setOperation(null);
  }, []);

  const value = useMemo(
    () => ({
      operation,
      claimOperation,
      ownsOperation,
      releaseOperation,
    }),
    [claimOperation, operation, ownsOperation, releaseOperation],
  );

  return (
    <ResearchOperationContext.Provider value={value}>
      {children}
    </ResearchOperationContext.Provider>
  );
}

export function useResearchOperation(): ResearchOperationContextValue {
  const value = useContext(ResearchOperationContext);
  if (value === null) {
    throw new Error(
      "useResearchOperation must be used within ResearchOperationProvider",
    );
  }
  return value;
}
