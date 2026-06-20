"use client";

import { ErrorMessage } from "@/components/feedback/ErrorMessage";
import type { ErrorPageProps } from "@/lib/types/error-page";

export default function PipelineStatusError({
  error,
  unstable_retry,
}: ErrorPageProps) {
  return (
    <ErrorMessage
      title="Pipeline status の読み込みに失敗しました"
      description="しばらく経ってから再度お試しください"
      error={error}
      unstable_retry={unstable_retry}
    />
  );
}
