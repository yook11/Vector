"use client";

import { ErrorMessage } from "@/components/feedback/ErrorMessage";
import type { ErrorPageProps } from "@/lib/types/error-page";

export default function BriefingError({ error, reset }: ErrorPageProps) {
  return (
    <ErrorMessage
      title="ブリーフィング"
      description="ブリーフィングの取得に失敗しました"
      error={error}
      reset={reset}
    />
  );
}
