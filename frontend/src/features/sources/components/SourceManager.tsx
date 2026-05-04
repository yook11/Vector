import { Button } from "@/components/ui/button";
import type { NewsSourceDetail } from "@/types/types.gen";
import { SourceFormDialog } from "./SourceFormDialog";
import { SourceTable } from "./SourceTable";

interface SourceManagerProps {
  initialSources: NewsSourceDetail[];
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
