"use client";

import { ErrorMessage } from "@/components/feedback/ErrorMessage";

interface ErrorProps {
  error: Error & { digest?: string };
  reset: () => void;
}

export default function ProtectedError({ error, reset }: ErrorProps) {
  return (
    <ErrorMessage
      title="ページの読み込みに失敗しました"
      description="しばらく経ってから再度お試しください"
      error={error}
      reset={reset}
    />
  );
}
