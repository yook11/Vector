"use client";

import { ErrorMessage } from "@/components/feedback/ErrorMessage";
import type { ErrorPageProps } from "@/lib/types/error-page";

export default function TrendsError({ error, reset }: ErrorPageProps) {
  return (
    <ErrorMessage
      title="トレンド"
      description="トレンドの取得に失敗しました"
      error={error}
      reset={reset}
    />
  );
}
