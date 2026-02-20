"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { clientTriggerFetch as triggerFetch } from "@/lib/client-api";

export function FetchButton() {
  const router = useRouter();
  const [loading, setLoading] = useState(false);

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
