"use client";

import { ErrorMessage } from "@/components/feedback/ErrorMessage";
import type { ErrorPageProps } from "@/lib/types/error-page";

export default function WatchlistError({ error, reset }: ErrorPageProps) {
  return (
    <ErrorMessage
      title="ウォッチリストの取得に失敗しました"
      description="しばらく経ってから再度お試しください"
      error={error}
      reset={reset}
    />
  );
}
