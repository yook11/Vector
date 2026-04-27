"use client";

import { Button } from "@/components/ui/button";

interface ErrorProps {
  error: Error & { digest?: string };
  reset: () => void;
}

export default function WeeklyTrendsError({ reset }: ErrorProps) {
  return (
    <main className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl px-8 sm:px-12 py-6 sm:py-8 flex flex-col items-center justify-center gap-4 py-16">
        <h1 className="text-base font-medium">Weekly Trends</h1>
        <p className="text-sm text-muted-foreground">
          週次トレンドの取得に失敗しました
        </p>
        <Button variant="outline" size="sm" onClick={reset}>
          再試行
        </Button>
      </div>
    </main>
  );
}
