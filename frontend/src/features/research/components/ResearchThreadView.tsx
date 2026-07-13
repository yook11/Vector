import {
  AlertTriangle,
  Bot,
  ExternalLink,
  FileText,
  MessageSquareText,
} from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import type {
  ResearchAssistantMessage,
  ResearchMessageRun,
  ResearchThreadDetail,
  ResearchUserMessage,
} from "@/types/types.gen";
import { CitedAnswerContent } from "./CitedAnswerContent";
import { DeleteThreadButton } from "./DeleteThreadButton";
import { ResearchComposer } from "./ResearchComposer";
import { ResearchLiveAnnouncer } from "./ResearchLiveAnnouncer";
import {
  ResearchActiveRunBoundary,
  ResearchActiveRunDraft,
  ResearchActiveRunStatus,
  ResearchLiveScrollRegion,
} from "./ResearchThreadLiveBoundary";

type ResearchThreadMessage = ResearchUserMessage | ResearchAssistantMessage;

interface ResearchThreadViewProps {
  thread: ResearchThreadDetail;
}

function failedRunStatusText(run: ResearchMessageRun): string | null {
  if (run.status !== "failed") return null;
  switch (run.errorCode) {
    case "cancelled":
      return "キャンセルしました";
    case "enqueue_failed":
      return "実行キューに投入できませんでした";
    case "stale":
      return "時間切れになりました";
    case "generation_unavailable":
      return "回答を生成できませんでした";
    default:
      return "回答を生成できませんでした";
  }
}

function activeRunId(messages: ResearchThreadMessage[]): string | null {
  const active = messages.findLast(
    (message) =>
      message.role === "user" &&
      (message.run.status === "queued" || message.run.status === "running"),
  );
  return active?.role === "user" ? active.run.runId : null;
}

function completedRunIds(messages: ResearchThreadMessage[]): string[] {
  return messages.flatMap((message) =>
    message.role === "user" && message.run.status === "completed"
      ? [message.run.runId]
      : [],
  );
}

function finalAnswerContentKey(messages: ResearchThreadMessage[]): string {
  return messages
    .flatMap((message) =>
      message.role === "assistant"
        ? [`${message.seq}@${message.createdAt}`]
        : [],
    )
    .join("|");
}

function UserRunStatus({ run }: { run: ResearchMessageRun }) {
  const statusText = failedRunStatusText(run);
  if (!statusText) return null;
  return (
    <div className="mt-2 flex min-w-0 items-center gap-1.5 text-xs text-[var(--vector-ink-muted)]">
      <AlertTriangle aria-hidden="true" className="size-3.5 shrink-0" />
      <span className="min-w-0 break-words [overflow-wrap:anywhere]">
        {statusText}
      </span>
    </div>
  );
}

interface UserMessageProps {
  message: ResearchUserMessage;
  liveStatus?: ReactNode;
}

function UserMessage({ message, liveStatus }: UserMessageProps) {
  return (
    <article className="flex min-w-0 justify-end">
      <div className="min-w-0 max-w-[min(720px,92%)] rounded-md border border-[var(--vector-line)] bg-[var(--vector-paper)] px-4 py-3">
        <p className="whitespace-pre-wrap break-words text-sm leading-6 text-[var(--vector-ink)] [overflow-wrap:anywhere]">
          {message.content}
        </p>
        {liveStatus ?? <UserRunStatus run={message.run} />}
      </div>
    </article>
  );
}

function AssistantMessage({ message }: { message: ResearchAssistantMessage }) {
  return (
    <article className="flex min-w-0 gap-3">
      <div className="mt-1 flex size-8 shrink-0 items-center justify-center rounded-md bg-[var(--vector-accent-tint)] text-[var(--vector-accent-ink)]">
        <Bot aria-hidden="true" className="size-4" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="whitespace-pre-wrap break-words text-sm leading-7 text-[var(--vector-ink)] [overflow-wrap:anywhere]">
          <CitedAnswerContent
            content={message.content}
            sources={message.sources}
          />
        </div>
        {message.missingAspects.length > 0 && (
          <div className="mt-3 rounded-md border border-[var(--vector-rule)] bg-[var(--vector-paper)] px-3 py-2 text-xs text-[var(--vector-ink-muted)] break-words [overflow-wrap:anywhere]">
            {message.missingAspects.join(" / ")}
          </div>
        )}
        {message.sources.length > 0 && (
          <div className="mt-4 min-w-0 space-y-2">
            {message.sources.map((source) => (
              <div
                key={`${source.kind}-${source.sourceRef}`}
                className="min-w-0 rounded-md border border-[var(--vector-line)] bg-[var(--vector-surface)] px-3 py-2"
              >
                <div className="flex min-w-0 items-start gap-2">
                  <span className="mt-0.5 inline-flex h-5 min-w-5 items-center justify-center rounded-sm bg-[var(--vector-accent-tint)] px-1 text-[11px] font-semibold text-[var(--vector-accent-ink)]">
                    {source.sourceRef}
                  </span>
                  <div className="min-w-0 flex-1">
                    {source.kind === "external_url" ? (
                      <a
                        href={source.url}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex max-w-full min-w-0 items-center gap-1 text-sm font-medium text-[var(--vector-ink)] underline-offset-4 hover:underline"
                      >
                        <span className="min-w-0 truncate">{source.title}</span>
                        <ExternalLink
                          aria-hidden="true"
                          className="size-3.5 shrink-0"
                        />
                      </a>
                    ) : source.articleId !== null ? (
                      <Link
                        href={`/news/${source.articleId}`}
                        className="inline-flex max-w-full min-w-0 items-center gap-1 text-sm font-medium text-[var(--vector-ink)] underline-offset-4 hover:underline"
                      >
                        <span className="min-w-0 truncate">{source.title}</span>
                        <FileText
                          aria-hidden="true"
                          className="size-3.5 shrink-0"
                        />
                      </Link>
                    ) : (
                      <p className="max-w-full truncate text-sm font-medium text-[var(--vector-ink)]">
                        {source.title}
                      </p>
                    )}
                    {source.kind === "external_url" && (
                      <p className="mt-1 break-words text-xs leading-5 text-[var(--vector-ink-muted)] [overflow-wrap:anywhere]">
                        {source.evidenceClaim}
                      </p>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </article>
  );
}

interface ResearchMessageProps {
  message: ResearchThreadMessage;
  activeRunId: string | null;
}

function ResearchMessage({ message, activeRunId }: ResearchMessageProps) {
  if (message.role === "user") {
    if (
      message.run.runId === activeRunId &&
      (message.run.status === "queued" || message.run.status === "running")
    ) {
      return (
        <ResearchActiveRunBoundary
          runId={message.run.runId}
          initialStatus={message.run.status}
          initialStage={message.run.progressStage}
        >
          <UserMessage
            message={message}
            liveStatus={<ResearchActiveRunStatus />}
          />
          <ResearchActiveRunDraft />
        </ResearchActiveRunBoundary>
      );
    }
    return <UserMessage message={message} />;
  }
  return <AssistantMessage message={message} />;
}

export function ResearchThreadView({ thread }: ResearchThreadViewProps) {
  const currentRunId = activeRunId(thread.messages);
  const completedIds = completedRunIds(thread.messages);
  const finalContentKey = finalAnswerContentKey(thread.messages);
  return (
    <section className="flex min-h-0 min-w-0 flex-1 flex-col bg-[var(--vector-surface-2)]">
      <header className="flex items-center justify-between gap-3 border-b border-[var(--vector-rule)] bg-[var(--vector-surface)]/92 px-4 py-3">
        <div className="min-w-0">
          <p
            className="text-[11px] font-semibold uppercase text-[var(--vector-accent-ink)]"
            style={{ fontFamily: "var(--font-vector-display)" }}
          >
            THREAD
          </p>
          <h2 className="truncate text-lg font-semibold text-[var(--vector-ink)]">
            {thread.title}
          </h2>
        </div>
        <DeleteThreadButton threadId={thread.threadId} title={thread.title} />
      </header>
      <ResearchLiveAnnouncer
        threadId={thread.threadId}
        activeRunId={currentRunId}
        completedRunIds={completedIds}
      />
      <ResearchLiveScrollRegion finalContentKey={finalContentKey}>
        <div className="mx-auto flex max-w-[860px] min-w-0 flex-col gap-5">
          {thread.messages.map((message) => (
            <ResearchMessage
              key={`${message.role}-${message.seq}`}
              message={message}
              activeRunId={currentRunId}
            />
          ))}
        </div>
      </ResearchLiveScrollRegion>
      <ResearchComposer threadId={thread.threadId} activeRunId={currentRunId} />
    </section>
  );
}

export function ResearchEmptyView() {
  return (
    <section className="flex min-h-0 min-w-0 flex-1 flex-col bg-[var(--vector-surface-2)]">
      <div className="flex flex-1 items-center justify-center px-5 py-10">
        <div className="max-w-md text-center">
          <div className="mx-auto flex size-12 items-center justify-center rounded-md bg-[var(--vector-accent-tint)] text-[var(--vector-accent-ink)]">
            <MessageSquareText aria-hidden="true" className="size-5" />
          </div>
          <h2 className="mt-4 text-xl font-semibold text-[var(--vector-ink)]">
            リサーチを開始
          </h2>
          <p className="mt-2 text-sm leading-6 text-[var(--vector-ink-muted)]">
            企業、技術、市場の動向について質問できます。
          </p>
        </div>
      </div>
      <ResearchComposer activeRunId={null} />
    </section>
  );
}
