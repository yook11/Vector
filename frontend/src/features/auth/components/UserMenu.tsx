"use client";

import { Loader2Icon, LogOut } from "lucide-react";
import { useTransition } from "react";
import { Button } from "@/components/ui/button";
import { signOut, useSession } from "@/lib/auth/auth-client";
import { cn } from "@/lib/utils/cn";
import { toastError } from "@/lib/utils/toast-error";

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
  const [isSigningOut, startTransition] = useTransition();

  if (!session?.user) return null;

  // signOut は失敗時に throw せず { error } を返すため、戻り値と例外の両方を見る。
  // 成功時はフルリロードでセッションを確実に破棄する。
  const handleSignOut = () => {
    startTransition(async () => {
      try {
        const result = await signOut();
        if (result?.error) {
          toastError(result.error, "ログアウトに失敗しました");
          return;
        }
        window.location.href = "/auth/login";
      } catch (err) {
        toastError(err, "ログアウトに失敗しました");
      }
    });
  };

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
        onClick={handleSignOut}
        disabled={isSigningOut}
        aria-busy={isSigningOut}
      >
        {isSigningOut ? (
          <Loader2Icon
            aria-hidden="true"
            className={cn(
              "size-4 animate-spin motion-reduce:animate-none",
              !compact && "mr-1",
            )}
          />
        ) : (
          <LogOut
            aria-hidden="true"
            className={cn("size-4", !compact && "mr-1")}
          />
        )}
        {!compact && buttonLabel}
        {compact && <span className="sr-only">{buttonLabel}</span>}
      </Button>
    </div>
  );
}
