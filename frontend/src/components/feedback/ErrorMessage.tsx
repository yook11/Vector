"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";

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
  const [retryAccepted, setRetryAccepted] = useState(false);

  useEffect(() => {
    console.error(error);
  }, [error]);

  const handleRetry = () => {
    setRetryAccepted(true);
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
          aria-busy={false}
        >
          再試行
        </Button>
        {retryAccepted && (
          <p
            role="status"
            aria-label="再試行を開始しました"
            aria-live="polite"
            aria-atomic="true"
          >
            再試行を開始しました
          </p>
        )}
      </div>
    </main>
  );
}
