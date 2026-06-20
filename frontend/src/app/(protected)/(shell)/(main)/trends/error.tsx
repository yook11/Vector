"use client";

import { ErrorMessage } from "@/components/feedback/ErrorMessage";
import type { ErrorPageProps } from "@/lib/types/error-page";

export default function TrendsError({ error, unstable_retry }: ErrorPageProps) {
  return (
    <ErrorMessage
      title="トレンド"
      description="トレンドの取得に失敗しました"
      error={error}
      unstable_retry={unstable_retry}
    />
  );
}
