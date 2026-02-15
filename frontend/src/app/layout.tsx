import type { Metadata } from "next";
import "./globals.css";

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
    <html lang="ja">
      <body>{children}</body>
    </html>
  );
}
