"use client";

import { Loader2Icon } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";

// unstable_retry は内部で別 transition を張って refetch するため、呼び出し側の
// useTransition では押下中フィードバックを保持できない (isPending が refetch を跨がない)。
// 押下が受理されたことを最低限可視化するための固定窓。
const RETRY_FEEDBACK_MS = 600;

interface ErrorMessageProps {
  title: string;
  description: string;
  error: Error & { digest?: string };
  // Next の reset() は再 render のみで再 fetch しないため、復帰には unstable_retry を使う。
  unstable_retry: () => void;
}

export function ErrorMessage({
  title,
  description,
  error,
  unstable_retry,
}: ErrorMessageProps) {
  const [isRetrying, setIsRetrying] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    console.error(error);
  }, [error]);

  useEffect(
    () => () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    },
    [],
  );

  const handleRetry = () => {
    setIsRetrying(true);
    timerRef.current = setTimeout(
      () => setIsRetrying(false),
      RETRY_FEEDBACK_MS,
    );
    unstable_retry();
  };

  return (
    <main className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-8 sm:px-12 py-16 flex flex-col items-center justify-center gap-4">
        <h1 className="text-base font-medium">{title}</h1>
        <p className="text-sm text-muted-foreground">{description}</p>
        <Button
          variant="outline"
          size="sm"
          onClick={handleRetry}
          disabled={isRetrying}
          aria-busy={isRetrying}
        >
          {isRetrying && (
            <Loader2Icon aria-hidden="true" className="animate-spin" />
          )}
          {isRetrying ? "再試行中…" : "再試行"}
        </Button>
      </div>
    </main>
  );
}
