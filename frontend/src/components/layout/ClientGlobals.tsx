"use client";

import dynamic from "next/dynamic";

// Defer non-critical client-only globals so they don't block initial paint.
// Sonner runs only in the browser anyway, so ssr:false avoids paying for
// hydration of placeholders during SSR.
const Toaster = dynamic(
  () => import("sonner").then((m) => ({ default: m.Toaster })),
  { ssr: false },
);

export function ClientGlobals() {
  return <Toaster richColors position="bottom-right" />;
}
