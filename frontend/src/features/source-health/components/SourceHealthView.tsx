import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatDate } from "@/lib/date";
import { cn } from "@/lib/utils/cn";
import type { SourceHealthItem, SourceHealthResponse } from "@/types/types.gen";
import { hoursToWindow } from "../window";

interface SourceHealthViewProps {
  data: SourceHealthResponse;
}

const COLUMNS: Array<{ label: string; align: "left" | "right" }> = [
  { label: "Source", align: "left" },
  { label: "Analyzable rate", align: "right" },
  { label: "Analyzable", align: "right" },
  { label: "Incomplete", align: "right" },
  { label: "Failure reasons", align: "left" },
  { label: "Last succeeded", align: "left" },
];

/**
 * source 別 health を装飾・判定なしでそのまま表示する presentational view。
 *
 * 行は backend の配列順 (sourceName 昇順) のまま全件描画し、フィルタ/隠蔽はしない。
 * 値の整形 (rate → "N%"、datetime → locale、null → "-") のみ行い、健全/異常の判定色は
 * 付けない。意味を持たない 0/null だけを淡色にする。failure reasons は全件縦積みし、
 * 省略 (truncate / "+N more") をしない。
 */
export function SourceHealthView({ data }: SourceHealthViewProps) {
  return (
    <div className="flex flex-col gap-4">
      <p className="text-xs text-muted-foreground tabular-nums">
        {hoursToWindow(data.windowHours)} window · observed{" "}
        {formatDate(data.observedAt, { withTime: true })}
      </p>
      <Table>
        <TableHeader>
          <TableRow>
            {COLUMNS.map((col) => (
              <TableHead
                key={col.label}
                className={col.align === "right" ? "text-right" : undefined}
              >
                {col.label}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {data.items.length === 0 ? (
            <TableRow>
              <TableCell
                colSpan={COLUMNS.length}
                className="text-center text-muted-foreground"
              >
                No sources.
              </TableCell>
            </TableRow>
          ) : (
            data.items.map((item) => (
              <SourceRow key={item.sourceId} item={item} />
            ))
          )}
        </TableBody>
      </Table>
    </div>
  );
}

function SourceRow({ item }: { item: SourceHealthItem }) {
  return (
    <TableRow>
      <TableCell>
        <div className="flex flex-col gap-1">
          <span className="font-medium">{item.sourceName}</span>
          <div className="flex items-center gap-1.5">
            <Badge variant="outline" className="font-mono">
              {item.sourceType}
            </Badge>
            <Badge
              variant={item.isActive ? "secondary" : "outline"}
              className={cn(!item.isActive && "text-muted-foreground")}
            >
              {item.isActive ? "active" : "inactive"}
            </Badge>
          </div>
        </div>
      </TableCell>
      <TableCell
        className={cn(
          "text-right tabular-nums",
          item.analyzableRate === null && "text-muted-foreground",
        )}
      >
        {item.analyzableRate === null ? "-" : `${item.analyzableRate}%`}
      </TableCell>
      <TableCell
        className={cn(
          "text-right tabular-nums",
          item.processedArticleCount === 0 && "text-muted-foreground",
        )}
      >
        {`${item.analyzableCount} / ${item.processedArticleCount}`}
      </TableCell>
      <TableCell
        className={cn(
          "text-right tabular-nums",
          item.incompleteCount === 0 && "text-muted-foreground",
        )}
      >
        {item.incompleteCount}
      </TableCell>
      <TableCell>
        <FailureReasons reasons={item.failureReasons} />
      </TableCell>
      <TableCell
        className={cn(item.lastSucceededAt === null && "text-muted-foreground")}
      >
        {item.lastSucceededAt === null
          ? "-"
          : formatDate(item.lastSucceededAt, { withTime: true })}
      </TableCell>
    </TableRow>
  );
}

function FailureReasons({
  reasons,
}: {
  reasons: SourceHealthItem["failureReasons"];
}) {
  if (reasons.length === 0) {
    return <span className="text-muted-foreground">-</span>;
  }
  return (
    <ul className="flex flex-col gap-1">
      {reasons.map((reason) => (
        <li key={reason.outcomeCode} className="flex items-center gap-2">
          <code className="text-xs">{reason.outcomeCode}</code>
          <span className="tabular-nums text-muted-foreground">
            {reason.count}
          </span>
        </li>
      ))}
    </ul>
  );
}
