import type { Metadata, Viewport } from "next";
import { Plus_Jakarta_Sans } from "next/font/google";
import { Suspense } from "react";
import { ClientGlobals } from "@/components/layout/ClientGlobals";
import { NonceThemeProvider } from "@/components/layout/NonceThemeProvider";
import "./globals.css";

const plusJakartaSans = Plus_Jakarta_Sans({
  subsets: ["latin"],
  variable: "--font-sans",
  display: "swap",
});

// OG/Twitter 画像の絶対 URL 解決に metadataBase が要る。frontend の公開オリジンは
// BETTER_AUTH_URL (fly.toml [env]) を使う。FRONTEND_URL は backend (vector-core) の
// CORS 用 var で frontend env には届かないため使わない。未設定の dev は localhost。
export const metadata: Metadata = {
  metadataBase: new URL(process.env.BETTER_AUTH_URL ?? "http://localhost:3000"),
  title: {
    default: "Vector",
    template: "%s | Vector",
  },
  description: "Tech news aggregation & AI analysis dashboard",
  applicationName: "Vector",
  openGraph: {
    title: "Vector",
    description: "Tech news aggregation & AI analysis dashboard",
    siteName: "Vector",
    type: "website",
  },
  twitter: {
    card: "summary_large_image",
    title: "Vector",
    description: "Tech news aggregation & AI analysis dashboard",
  },
};

// PWA / モバイル UI のテーマ色 (アイコン背景に合わせる)。
export const viewport: Viewport = {
  themeColor: "#0FA89C",
};

// Header は `(protected)/layout.tsx` 側に配置している。auth page (login/register)
// では Header が表示されないことが UX 要件 (PR-Z12)。Next.js は子 layout から親
// layout の出力を抑制できないため、root layout には Header を置かず、Header を
// 必要とする route group の layout に閉じ込める方針を採る。
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="ja" suppressHydrationWarning>
      <body
        className={`${plusJakartaSans.variable} font-sans bg-background text-foreground`}
      >
        <Suspense>
          <NonceThemeProvider>
            {children}
            <ClientGlobals />
          </NonceThemeProvider>
        </Suspense>
      </body>
    </html>
  );
}
