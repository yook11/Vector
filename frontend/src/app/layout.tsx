import type { Metadata, Viewport } from "next";
import {
  Big_Shoulders,
  Newsreader,
  Plus_Jakarta_Sans,
  Shippori_Mincho_B1,
  Zen_Kaku_Gothic_New,
  Zen_Maru_Gothic,
} from "next/font/google";
import { Suspense } from "react";
import { ClientGlobals } from "@/components/layout/ClientGlobals";
import { NonceThemeProvider } from "@/components/layout/NonceThemeProvider";
import "./globals.css";

const plusJakartaSans = Plus_Jakarta_Sans({
  subsets: ["latin"],
  variable: "--font-sans",
  display: "swap",
});

const vectorWordmark = Big_Shoulders({
  subsets: ["latin"],
  weight: ["500", "600", "700"],
  variable: "--font-vector-wordmark",
  display: "swap",
});

const vectorSerif = Shippori_Mincho_B1({
  subsets: ["latin"],
  weight: ["500", "700", "800"],
  variable: "--font-vector-serif",
  display: "swap",
});

const vectorSans = Zen_Kaku_Gothic_New({
  subsets: ["latin"],
  weight: ["400", "500", "700"],
  variable: "--font-vector-sans",
  display: "swap",
});

const vectorMaru = Zen_Maru_Gothic({
  subsets: ["latin"],
  weight: ["500", "700", "900"],
  variable: "--font-vector-maru",
  display: "swap",
});

const vectorDisplay = Newsreader({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  style: ["normal", "italic"],
  variable: "--font-vector-display",
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
        className={`${plusJakartaSans.variable} ${vectorWordmark.variable} ${vectorSerif.variable} ${vectorSans.variable} ${vectorMaru.variable} ${vectorDisplay.variable} font-sans bg-background text-foreground`}
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
