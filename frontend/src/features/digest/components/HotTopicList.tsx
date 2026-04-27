import type { WeeklyTopicTrend } from "@/types";

interface HotTopicListProps {
  topics: WeeklyTopicTrend[];
}

export function HotTopicList({ topics }: HotTopicListProps) {
  if (topics.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">
        該当するトピックはありません
      </p>
    );
  }

  return (
    <ul className="flex flex-col divide-y divide-border">
      {topics.map((topic) => (
        <li
          key={topic.topic}
          className="flex items-baseline justify-between gap-3 py-2.5"
        >
          <span className="text-sm font-medium text-foreground truncate">
            {topic.topic}
          </span>
          <div className="shrink-0 flex items-baseline gap-2 text-xs text-muted-foreground tabular-nums">
            <span>
              {topic.previousCount} → {topic.currentCount}
            </span>
            <span className="font-medium text-foreground">
              ×{topic.hotnessScore.toFixed(1)}
            </span>
          </div>
        </li>
      ))}
    </ul>
  );
}
