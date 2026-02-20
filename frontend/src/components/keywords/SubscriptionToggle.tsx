"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { clientSubscribe, clientUnsubscribe } from "@/lib/client-api";
import { useRouter } from "next/navigation";

interface SubscriptionToggleProps {
  keywordId: number;
  isSubscribed: boolean;
}

export function SubscriptionToggle({
  keywordId,
  isSubscribed: initialIsSubscribed,
}: SubscriptionToggleProps) {
  const [isSubscribed, setIsSubscribed] = useState(initialIsSubscribed);
  const [pending, setPending] = useState(false);
  const router = useRouter();

  async function handleToggle() {
    setPending(true);
    try {
      if (isSubscribed) {
        await clientUnsubscribe(keywordId);
        setIsSubscribed(false);
      } else {
        await clientSubscribe(keywordId);
        setIsSubscribed(true);
      }
      router.refresh();
    } catch {
      // Revert on error
    } finally {
      setPending(false);
    }
  }

  return (
    <Button
      variant={isSubscribed ? "default" : "outline"}
      size="sm"
      onClick={handleToggle}
      disabled={pending}
    >
      {isSubscribed ? "Subscribed" : "Subscribe"}
    </Button>
  );
}
