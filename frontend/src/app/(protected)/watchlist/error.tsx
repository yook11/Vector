"use client";

import { ErrorMessage } from "@/components/feedback/ErrorMessage";

interface ErrorProps {
  error: Error & { digest?: string };
  reset: () => void;
}

export default function WatchlistError({ error, reset }: ErrorProps) {
  return (
    <ErrorMessage
      title="ウォッチリストの取得に失敗しました"
      description="しばらく経ってから再度お試しください"
      error={error}
      reset={reset}
    />
  );
}
