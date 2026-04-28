"use client";

import dynamic from "next/dynamic";

// Defer non-critical client-only globals so they don't block initial paint.
// Sonner runs only in the browser anyway, so ssr:false avoids paying for
// hydration of placeholders during SSR.
//
// Toaster は @/components/ui/sonner ラッパーを経由する。これにより
//   - next-themes と連動 (light/dark 自動切替)
//   - popover token (--normal-bg / --normal-text / --normal-border) を使用
//   - lucide アイコンセットがトーストに付く
// が手に入る。raw sonner を直接 import すると上記が抜け落ちる。
const Toaster = dynamic(
  () => import("@/components/ui/sonner").then((m) => ({ default: m.Toaster })),
  { ssr: false },
);

export function ClientGlobals() {
  return <Toaster richColors position="bottom-right" />;
}
