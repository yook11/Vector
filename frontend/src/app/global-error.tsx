"use client";

import { useEffect } from "react";
import type { ErrorPageProps } from "@/lib/types/error-page";

// global-error.tsx は RootLayout の throw を catch するため、自前で
// <html>/<body> を返す必要がある。下層 layout の error.tsx で吸収しきれな
// かった catastrophic error の最終フォールバック。
export default function GlobalError({ error, reset }: ErrorPageProps) {
  useEffect(() => {
    console.error(error);
  }, [error]);

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
            onClick={reset}
            style={{
              border: "1px solid #d1d5db",
              borderRadius: "0.375rem",
              padding: "0.5rem 1rem",
              fontSize: "0.875rem",
              background: "transparent",
              cursor: "pointer",
            }}
          >
            再試行
          </button>
        </main>
      </body>
    </html>
  );
}
