import type { Metadata } from "next";
import { Plus_Jakarta_Sans } from "next/font/google";
import { Suspense } from "react";
import { ClientGlobals } from "@/components/layout/ClientGlobals";
import { Header } from "@/components/layout/Header";
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
            <Header />
            <div className="mt-11 h-[calc(100dvh-2.75rem)]">{children}</div>
            <ClientGlobals />
          </NonceThemeProvider>
        </Suspense>
      </body>
    </html>
  );
}
