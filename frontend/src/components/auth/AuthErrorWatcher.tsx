"use client";

import { useEffect } from "react";
import { signOut, useSession } from "@/lib/auth-client";

/**
 * Watches the session error state and automatically signs out when
 * the session is invalid. This handles the case where SPA
 * navigation occurs without a full page reload (no SSR re-execution).
 */
export function AuthErrorWatcher() {
  const { error } = useSession();

  useEffect(() => {
    if (error) {
      signOut().then(() => {
        window.location.href = "/auth/login";
      });
    }
  }, [error]);

  return null;
}
