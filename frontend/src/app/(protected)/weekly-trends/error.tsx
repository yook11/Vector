"use client";

import { ErrorMessage } from "@/components/feedback/ErrorMessage";
import type { ErrorPageProps } from "@/lib/types/error-page";

export default function WeeklyTrendsError({ error, reset }: ErrorPageProps) {
  return (
    <ErrorMessage
      title="Weekly Trends"
      description="週次トレンドの取得に失敗しました"
      error={error}
      reset={reset}
    />
  );
}
