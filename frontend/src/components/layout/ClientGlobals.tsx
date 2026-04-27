"use client";

import dynamic from "next/dynamic";

// Defer non-critical client-only globals so they don't block initial paint.
// Both run only in the browser anyway, so ssr:false avoids paying for hydration
// of placeholders during SSR.
const Toaster = dynamic(
  () => import("sonner").then((m) => ({ default: m.Toaster })),
  { ssr: false },
);

const AuthErrorWatcher = dynamic(
  () =>
    import("@/components/auth/AuthErrorWatcher").then((m) => ({
      default: m.AuthErrorWatcher,
    })),
  { ssr: false },
);

export function ClientGlobals() {
  return (
    <>
      <AuthErrorWatcher />
      <Toaster richColors position="bottom-right" />
    </>
  );
}
