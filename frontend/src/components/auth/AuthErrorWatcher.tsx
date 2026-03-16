"use client";

import { signOut, useSession } from "next-auth/react";
import { useEffect } from "react";

/**
 * Watches the session error flag and automatically signs out when
 * the refresh token has failed. This handles the case where SPA
 * navigation occurs without a full page reload (no SSR re-execution).
 */
export function AuthErrorWatcher() {
  const { data: session } = useSession();

  useEffect(() => {
    if (session?.error === "RefreshTokenError") {
      signOut({ callbackUrl: "/auth/login" });
    }
  }, [session?.error]);

  return null;
}
