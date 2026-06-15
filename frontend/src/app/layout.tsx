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

// next/font/google は latin subset しか self-host せず、日本語グリフは web font に
// 含まれない (CSS2 API が subset 指定なしでは CJK の動的 slice を返さない)。
// 各 fontFamily は var(--font-vector-*) を直接参照するため、ローダの fallback に
// system CJK を渡せば生成 CSS 変数へ連結され (loaded → adjustFontFallback → fallback)、
// 全箇所の日本語が端末標準フォントで描画される。字種別に明朝/角ゴ/丸ゴを当てる。
// next/font の引数は静的リテラル必須のため fallback 配列は各 loader に直接書く
// (定数参照は "Font loader values must be explicitly written literals" で build error)。
const plusJakartaSans = Plus_Jakarta_Sans({
  subsets: ["latin"],
  variable: "--font-sans",
  display: "swap",
  fallback: [
    "Hiragino Sans",
    "Hiragino Kaku Gothic ProN",
    "Noto Sans JP",
    "Yu Gothic",
    "YuGothic",
    "Meiryo",
    "sans-serif",
  ],
});

// 装飾 latin。日本語には使わない brand wordmark なので CJK fallback 不要。
// masthead で全認証ページの可視域上部に常時出る latin brand なので preload は維持し、
// 初回の FOUT を避ける (auth ページでは未使用=死荷重だが 2 ページのみで許容)。
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
  preload: false,
  fallback: [
    "Hiragino Mincho ProN",
    "Hiragino Mincho Pro",
    "Noto Serif JP",
    "Yu Mincho",
    "YuMincho",
    "serif",
  ],
});

const vectorSans = Zen_Kaku_Gothic_New({
  subsets: ["latin"],
  weight: ["400", "500", "700"],
  variable: "--font-vector-sans",
  display: "swap",
  preload: false,
  fallback: [
    "Hiragino Sans",
    "Hiragino Kaku Gothic ProN",
    "Noto Sans JP",
    "Yu Gothic",
    "YuGothic",
    "Meiryo",
    "sans-serif",
  ],
});

const vectorMaru = Zen_Maru_Gothic({
  subsets: ["latin"],
  weight: ["500", "700", "900"],
  variable: "--font-vector-maru",
  display: "swap",
  preload: false,
  fallback: [
    "Hiragino Maru Gothic ProN",
    "Hiragino Sans",
    "Noto Sans JP",
    "Yu Gothic",
    "Meiryo",
    "sans-serif",
  ],
});

// Newsreader は番号・日付など主に latin に当てる display serif。
// 稀に載る日本語は明朝系へ落とすため明朝系 CJK fallback を付与する。
const vectorDisplay = Newsreader({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  style: ["normal", "italic"],
  variable: "--font-vector-display",
  display: "swap",
  preload: false,
  fallback: [
    "Hiragino Mincho ProN",
    "Hiragino Mincho Pro",
    "Noto Serif JP",
    "Yu Mincho",
    "YuMincho",
    "serif",
  ],
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
