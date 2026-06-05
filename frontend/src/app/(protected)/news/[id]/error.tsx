"use client";

import { ErrorMessage } from "@/components/feedback/ErrorMessage";
import type { ErrorPageProps } from "@/lib/types/error-page";

export default function NewsDetailError({ error, reset }: ErrorPageProps) {
  return (
    <ErrorMessage
      title="記事の取得に失敗しました"
      description="記事が一時的に表示できません。再試行するか、一覧に戻ってください"
      error={error}
      reset={reset}
    />
  );
}
