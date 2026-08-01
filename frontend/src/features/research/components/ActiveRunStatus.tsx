import { Loader2 } from "lucide-react";
import type { ResearchRunResponse } from "@/types/types.gen";
import type { ResearchLiveActivity, ResearchLiveStage } from "../live/events";

type ActiveRunStatusValue = Extract<
  ResearchRunResponse["status"],
  "queued" | "running"
>;

interface ActiveRunStatusProps {
  status: ActiveRunStatusValue;
  stage: ResearchLiveStage | null;
  activity: ResearchLiveActivity | null;
}

export function activeRunText(
  status: ActiveRunStatusValue,
  stage: ResearchLiveStage | null,
): string {
  if (status === "queued") return "待機中";
  switch (stage) {
    case "safety_check":
      return "検証中";
    case "context_resolution":
      return "コンテキストを整理中";
    case "planning":
      return "計画中";
    case "evidence_collection":
      return "情報収集中";
    case "evidence_review":
      return "情報を選別中";
    case "answering":
      return "回答作成中";
    default:
      return "生成中";
  }
}

function evidenceActivityText(activity: ResearchLiveActivity): string | null {
  switch (activity.type) {
    case "evidence_collection.internal_search_started":
      return "関連記事を検索中";
    case "evidence_collection.internal_search_completed":
      return `関連記事${activity.hitCount}件を確認`;
    case "evidence_collection.external_search_queries_generated":
      if (activity.queries.length === 0) return null;
      if (activity.queries.length === 1) {
        return `“${activity.queries[0]}” を検索中`;
      }
      return `“${activity.queries[0]}” など${activity.queries.length}件を検索中`;
    case "evidence_collection.external_search_candidates_fetched":
      return `候補${activity.candidateCount}件を取得`;
    case "evidence_review.selected":
      return `根拠${activity.evidenceCount}件を選別`;
    case "context_resolution.question_resolved":
      return null;
  }
}

function activityText(
  status: ActiveRunStatusValue,
  stage: ResearchLiveStage | null,
  activity: ResearchLiveActivity | null,
): string | null {
  if (status !== "running" || activity === null) return null;
  // 根拠の選別は精査工程で発火するため、収集と精査を同じ表示区間として扱う。
  if (stage === "evidence_collection" || stage === "evidence_review") {
    return evidenceActivityText(activity);
  }
  if (stage === "answering") return null;
  return activity.type === "context_resolution.question_resolved"
    ? `“${activity.standaloneQuestion}”について調査中`
    : null;
}

export function ActiveRunStatus({
  status,
  stage,
  activity,
}: ActiveRunStatusProps) {
  const detail = activityText(status, stage, activity);

  return (
    <div className="mt-2 min-w-0 text-xs text-[var(--vector-ink-muted)]">
      <div className="flex min-w-0 items-center gap-1.5">
        <Loader2
          aria-hidden="true"
          className="size-3.5 shrink-0 animate-spin motion-reduce:animate-none"
        />
        <span className="min-w-0 whitespace-nowrap">
          {activeRunText(status, stage)}
        </span>
      </div>
      {detail ? (
        <p className="mt-0.5 line-clamp-2 min-w-0 break-words pl-5 text-[11px] leading-4 text-[var(--vector-ink-soft)] [overflow-wrap:anywhere]">
          {detail}
        </p>
      ) : null}
    </div>
  );
}
