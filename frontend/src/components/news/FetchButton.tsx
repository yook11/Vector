"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { useSession } from "@/lib/auth-client";
import { clientTriggerFetch as triggerFetch } from "@/lib/client-api";

export function FetchButton() {
  const { data: session } = useSession();
  const router = useRouter();
  const [loading, setLoading] = useState(false);

  if ((session?.user as Record<string, unknown> | undefined)?.role !== "admin")
    return null;

  async function handleFetch() {
    setLoading(true);
    try {
      const res = await triggerFetch();
      toast.success(res.message);
      router.refresh();
    } catch {
      toast.error("Failed to trigger fetch");
    } finally {
      setLoading(false);
    }
  }

  return (
    <Button onClick={handleFetch} disabled={loading} size="sm">
      {loading ? "Fetching..." : "Fetch News"}
    </Button>
  );
}
