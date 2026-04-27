"use client";

import { LogOut } from "lucide-react";
import { Button } from "@/components/ui/button";
import { signOut, useSession } from "@/lib/auth/auth-client";

export function UserMenu() {
  const { data: session } = useSession();

  if (!session?.user) return null;

  return (
    <div className="flex items-center gap-3">
      <span className="text-sm text-muted-foreground">
        {session.user.email}
      </span>
      <Button
        variant="ghost"
        size="sm"
        onClick={async () => {
          await signOut();
          window.location.href = "/auth/login";
        }}
      >
        <LogOut aria-hidden="true" className="h-4 w-4 mr-1" />
        Sign out
      </Button>
    </div>
  );
}
