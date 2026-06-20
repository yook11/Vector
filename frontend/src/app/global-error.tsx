"use client";

import { useEffect, useRef, useState } from "react";
import type { ErrorPageProps } from "@/lib/types/error-page";

// global-error.tsx は RootLayout の throw を catch するため、自前で
// <html>/<body> を返す必要がある。下層 layout の error.tsx で吸収しきれな
// かった catastrophic error の最終フォールバック。
// Tailwind/lucide が確実に効かない境界のため、pending は inline style と文言で表す。
// unstable_retry は内部 transition で refetch するため完了を観測できず、押下受理を
// 最低限可視化する固定窓で pending を表現する (ErrorMessage と同方針)。
const RETRY_FEEDBACK_MS = 600;

export default function GlobalError({ error, unstable_retry }: ErrorPageProps) {
  const [isRetrying, setIsRetrying] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    console.error(error);
  }, [error]);

  useEffect(
    () => () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    },
    [],
  );

  const handleRetry = () => {
    setIsRetrying(true);
    timerRef.current = setTimeout(
      () => setIsRetrying(false),
      RETRY_FEEDBACK_MS,
    );
    unstable_retry();
  };

  return (
    <html lang="ja">
      <body>
        <main
          style={{
            minHeight: "100dvh",
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            gap: "1rem",
            padding: "1rem",
            fontFamily:
              "system-ui, -apple-system, BlinkMacSystemFont, sans-serif",
          }}
        >
          <h1 style={{ fontSize: "1.125rem", fontWeight: 500 }}>
            予期しないエラーが発生しました
          </h1>
          <p style={{ fontSize: "0.875rem", color: "#6b7280" }}>
            ページの再読み込みをお試しください
          </p>
          <button
            type="button"
            onClick={handleRetry}
            disabled={isRetrying}
            aria-busy={isRetrying}
            style={{
              border: "1px solid #d1d5db",
              borderRadius: "0.375rem",
              padding: "0.5rem 1rem",
              fontSize: "0.875rem",
              background: "transparent",
              cursor: isRetrying ? "default" : "pointer",
              opacity: isRetrying ? 0.6 : 1,
            }}
          >
            {isRetrying ? "再試行中…" : "再試行"}
          </button>
        </main>
      </body>
    </html>
  );
}
