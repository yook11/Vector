"use client";

import { Button } from "@/components/ui/button";
import type { NewsSourceResponse } from "@/types";
import { SourceTable } from "./SourceTable";
import { SourceFormDialog } from "./SourceFormDialog";

interface SourceManagerProps {
  initialSources: NewsSourceResponse[];
}

export function SourceManager({ initialSources }: SourceManagerProps) {
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold">News Sources</h2>
          <p className="text-sm text-muted-foreground">
            Manage RSS feeds and API sources for article fetching.
          </p>
        </div>
        <SourceFormDialog trigger={<Button>Add Source</Button>} />
      </div>
      <SourceTable sources={initialSources} />
    </div>
  );
}
