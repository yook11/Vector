import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatDate } from "@/lib/date";
import { formatAgeSeconds } from "@/lib/duration";
import { cn } from "@/lib/utils/cn";
import type {
  PipelineHealthResponse,
  PipelineHealthSummary,
  PipelineStageHealth,
} from "@/types/types.gen";

interface PipelineStatusViewProps {
  data: PipelineHealthResponse;
}

/**
 * pipeline health snapshot を装飾・判定なしでそのまま表示する presentational view。
 *
 * summary は key/value、stages は backend の配列順そのままの薄い table。値の整形
 * (age → "1h 12m"、datetime → locale、null → "-") のみ行い、healthy/warning の
 * 判定色は付けない。意味を持たない queue/backfill の 0/null だけを淡色にする。
 */
export function PipelineStatusView({ data }: PipelineStatusViewProps) {
  return (
    <div className="flex flex-col gap-8">
      <SummarySection summary={data.summary} />
      <StagesTable stages={data.stages} />
    </div>
  );
}

function SummarySection({ summary }: { summary: PipelineHealthSummary }) {
  // queue/backfill の値が意味を持たない 0/null のみ淡色にする (failed/datetime は除く)。
  const items: Array<{ label: string; value: string; muted: boolean }> = [
    {
      label: "Failed events (24h)",
      value: String(summary.failedEventCount24h),
      muted: false,
    },
    {
      label: "Backfill targets",
      value: String(summary.backfillTargetTotal),
      muted: summary.backfillTargetTotal === 0,
    },
    {
      label: "Oldest backfill age",
      value: formatAgeSeconds(summary.oldestBackfillTargetAgeSeconds),
      muted: summary.oldestBackfillTargetAgeSeconds === null,
    },
    {
      label: "Completion queue",
      value: String(summary.completionQueueCount),
      muted: summary.completionQueueCount === 0,
    },
    {
      label: "Oldest queue age",
      value: formatAgeSeconds(summary.oldestCompletionQueueAgeSeconds),
      muted: summary.oldestCompletionQueueAgeSeconds === null,
    },
    {
      label: "Observed at",
      value: formatDate(summary.observedAt, { withTime: true }),
      muted: false,
    },
    {
      label: "Event window start",
      value: formatDate(summary.eventWindowStart, { withTime: true }),
      muted: false,
    },
  ];

  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3">
      {items.map((item) => (
        <div key={item.label} className="flex flex-col gap-0.5">
          <dt className="text-xs text-muted-foreground">{item.label}</dt>
          <dd
            className={cn(
              "text-sm tabular-nums",
              item.muted && "text-muted-foreground",
            )}
          >
            {item.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

const STAGE_COLUMNS: Array<{ label: string; align: "left" | "right" }> = [
  { label: "Stage", align: "left" },
  { label: "Succeeded 24h", align: "right" },
  { label: "Failed 24h", align: "right" },
  { label: "Last succeeded", align: "left" },
  { label: "Queue count", align: "right" },
  { label: "Oldest queue age", align: "right" },
  { label: "Backfill targets", align: "right" },
  { label: "Oldest backfill age", align: "right" },
];

function StagesTable({ stages }: { stages: PipelineStageHealth[] }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {STAGE_COLUMNS.map((col) => (
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
        {stages.map((stage) => (
          <StageRow key={stage.stage} stage={stage} />
        ))}
      </TableBody>
    </Table>
  );
}

function StageRow({ stage }: { stage: PipelineStageHealth }) {
  return (
    <TableRow>
      <TableCell className="font-mono text-xs">{stage.stage}</TableCell>
      <TableCell className="text-right tabular-nums">
        {stage.succeededEventCount24h}
      </TableCell>
      <TableCell className="text-right tabular-nums">
        {stage.failedEventCount24h}
      </TableCell>
      <TableCell>
        {stage.lastSucceededAt === null
          ? "-"
          : formatDate(stage.lastSucceededAt, { withTime: true })}
      </TableCell>
      <NumericCell value={stage.queueCount} muted={stage.queueCount === 0} />
      <AgeCell seconds={stage.oldestQueueAgeSeconds} />
      <NumericCell
        value={stage.backfillTargetCount}
        muted={stage.backfillTargetCount === 0}
      />
      <AgeCell seconds={stage.oldestBackfillTargetAgeSeconds} />
    </TableRow>
  );
}

function NumericCell({ value, muted }: { value: number; muted: boolean }) {
  return (
    <TableCell
      className={cn(
        "text-right tabular-nums",
        muted && "text-muted-foreground",
      )}
    >
      {value}
    </TableCell>
  );
}

function AgeCell({ seconds }: { seconds: number | null }) {
  // age が無い (null) 軸は意味を持たないので淡色。
  return (
    <TableCell
      className={cn(
        "text-right tabular-nums",
        seconds === null && "text-muted-foreground",
      )}
    >
      {formatAgeSeconds(seconds)}
    </TableCell>
  );
}
