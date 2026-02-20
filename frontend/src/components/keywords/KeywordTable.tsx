import {
  Table,
  TableBody,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { KeywordRow } from "./KeywordRow";
import type { KeywordResponse } from "@/types";

interface KeywordTableProps {
  keywords: KeywordResponse[];
  subscribedKeywordIds?: Set<number>;
}

export function KeywordTable({
  keywords,
  subscribedKeywordIds,
}: KeywordTableProps) {
  if (keywords.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
        <p className="text-lg font-medium">No keywords yet</p>
        <p className="text-sm">Add a keyword to start tracking news.</p>
      </div>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Keyword</TableHead>
          <TableHead>Category</TableHead>
          <TableHead className="text-center">Articles</TableHead>
          <TableHead className="text-center">Active</TableHead>
          {subscribedKeywordIds && (
            <TableHead className="text-center">Subscribe</TableHead>
          )}
          <TableHead className="text-right">Actions</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {keywords.map((kw) => (
          <KeywordRow
            key={kw.id}
            keyword={kw}
            subscribedKeywordIds={subscribedKeywordIds}
          />
        ))}
      </TableBody>
    </Table>
  );
}
