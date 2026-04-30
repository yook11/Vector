import type { Metadata } from "next";
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

export const metadata: Metadata = {
  title: "Vector",
  description: "Tech news aggregation & AI analysis dashboard",
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
