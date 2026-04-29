"use client";

import { ErrorMessage } from "@/components/feedback/ErrorMessage";

interface ErrorProps {
  error: Error & { digest?: string };
  reset: () => void;
}

export default function SettingsError({ error, reset }: ErrorProps) {
  return (
    <ErrorMessage
      title="設定の読み込みに失敗しました"
      description="しばらく経ってから再度お試しください"
      error={error}
      reset={reset}
    />
  );
}
