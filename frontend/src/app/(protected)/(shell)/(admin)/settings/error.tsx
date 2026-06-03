"use client";

import { ErrorMessage } from "@/components/feedback/ErrorMessage";
import type { ErrorPageProps } from "@/lib/types/error-page";

export default function SettingsError({ error, reset }: ErrorPageProps) {
  return (
    <ErrorMessage
      title="設定の読み込みに失敗しました"
      description="しばらく経ってから再度お試しください"
      error={error}
      reset={reset}
    />
  );
}
