"use client";

import { LogOut } from "lucide-react";
import { Button } from "@/components/ui/button";
import { signOut, useSession } from "@/lib/auth/auth-client";
import { cn } from "@/lib/utils/cn";

interface UserMenuProps {
  className?: string;
  emailClassName?: string;
  buttonClassName?: string;
  buttonLabel?: string;
  compact?: boolean;
}

export function UserMenu({
  className,
  emailClassName,
  buttonClassName,
  buttonLabel = "Sign out",
  compact = false,
}: UserMenuProps) {
  const { data: session } = useSession();

  if (!session?.user) return null;

  return (
    <div className={cn("flex items-center gap-3", className)}>
      <span
        className={cn(
          "text-sm text-muted-foreground",
          compact && "max-w-40 truncate text-xs",
          emailClassName,
        )}
      >
        {session.user.email}
      </span>
      <Button
        variant="ghost"
        size={compact ? "icon-sm" : "sm"}
        className={buttonClassName}
        onClick={async () => {
          await signOut();
          window.location.href = "/auth/login";
        }}
      >
        <LogOut
          aria-hidden="true"
          className={cn("size-4", !compact && "mr-1")}
        />
        {!compact && buttonLabel}
        {compact && <span className="sr-only">{buttonLabel}</span>}
      </Button>
    </div>
  );
}
