import { MessageSquareText } from "lucide-react";
import type {
  ResearchAssistantMessage,
  ResearchThreadDetail,
  ResearchUserMessage,
} from "@/types/types.gen";
import { DeleteThreadButton } from "./DeleteThreadButton";
import { ResearchAnswerSlot } from "./ResearchAnswerSlot";
import { ResearchComposer } from "./ResearchComposer";
import {
  ResearchLiveAnnouncementBoundary,
  ResearchLiveAnnouncer,
} from "./ResearchLiveAnnouncer";
import { ResearchSourcesPanel } from "./ResearchSourcesPanel";
import {
  ResearchActiveRunBoundary,
  ResearchLiveScrollRegion,
  ResearchRunAnswerSlot,
  ResearchRunStatusRail,
} from "./ResearchThreadLiveBoundary";

type ResearchThreadMessage = ResearchUserMessage | ResearchAssistantMessage;

interface ResearchThreadViewProps {
  thread: ResearchThreadDetail;
  withSourcesPanel?: boolean;
}

function activeRunId(messages: ResearchThreadMessage[]): string | null {
  const active = messages.findLast(
    (message) =>
      message.role === "user" &&
      (message.run.status === "queued" || message.run.status === "running"),
  );
  return active?.role === "user" ? active.run.runId : null;
}

function failedAnnouncementRunId(
  messages: ResearchThreadMessage[],
): string | null {
  const latestUserMessage = messages.findLast(
    (message) => message.role === "user",
  );
  return latestUserMessage?.role === "user" &&
    latestUserMessage.run.status === "failed"
    ? latestUserMessage.run.runId
    : null;
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

function UserMessage({ message }: { message: ResearchUserMessage }) {
  return (
    <article className="flex min-w-0 justify-end">
      <div className="min-w-0 max-w-[min(720px,92%)] rounded-md border border-[var(--vector-line)] bg-[var(--vector-paper)] px-4 py-3">
        <p className="whitespace-pre-wrap break-words text-sm leading-6 text-[var(--vector-ink)] [overflow-wrap:anywhere]">
          {message.content}
        </p>
      </div>
    </article>
  );
}

interface ResearchTurnProps {
  userMessage: ResearchUserMessage;
  finalAnswer: ResearchAssistantMessage | null;
  activeRunId: string | null;
}

function ResearchTurn({
  userMessage,
  finalAnswer,
  activeRunId,
}: ResearchTurnProps) {
  const activeStatus =
    userMessage.run.runId === activeRunId &&
    (userMessage.run.status === "queued" ||
      userMessage.run.status === "running")
      ? userMessage.run.status
      : null;
  const isActive = activeStatus !== null;

  return (
    <ResearchActiveRunBoundary
      runId={userMessage.run.runId}
      createdAt={userMessage.createdAt}
      initialStatus={activeStatus}
      initialStage={userMessage.run.progressStage}
    >
      <div
        key="turn-presentation"
        data-research-answer-anchor
        data-research-turn-anchor
        data-research-run-id={userMessage.run.runId}
        data-research-persisted-status={userMessage.run.status}
        className="flex min-w-0 flex-col"
      >
        <UserMessage message={userMessage} />
        <ResearchRunStatusRail run={userMessage.run} isActive={isActive} />
        <ResearchRunAnswerSlot
          key="answer-slot"
          run={userMessage.run}
          isActive={isActive}
          finalAnswer={finalAnswer}
        />
      </div>
    </ResearchActiveRunBoundary>
  );
}

export function ResearchThreadView({
  thread,
  withSourcesPanel = false,
}: ResearchThreadViewProps) {
  const currentRunId = activeRunId(thread.messages);
  const announcementRunId =
    currentRunId ?? failedAnnouncementRunId(thread.messages);
  const completedIds = completedRunIds(thread.messages);
  const finalContentKey = finalAnswerContentKey(thread.messages);
  const answerPanel = (
    <ResearchLiveScrollRegion
      key="research-answer-panel"
      finalContentKey={finalContentKey}
    >
      <div
        key="research-answer-list"
        className="mx-auto flex max-w-[860px] min-w-0 flex-col gap-5"
      >
        {thread.messages.flatMap((message, index, messages) => {
          if (message.role === "user") {
            const nextMessage = messages[index + 1];
            const finalAnswer =
              nextMessage?.role === "assistant" ? nextMessage : null;
            return [
              <ResearchTurn
                key={`turn-${message.run.runId}`}
                userMessage={message}
                finalAnswer={finalAnswer}
                activeRunId={currentRunId}
              />,
            ];
          }
          if (messages[index - 1]?.role === "user") return [];
          return [
            <ResearchAnswerSlot
              key={`assistant-${message.seq}`}
              finalAnswer={message}
            />,
          ];
        })}
      </div>
    </ResearchLiveScrollRegion>
  );
  const headerLeading = (
    <div key="research-header-leading" className="min-w-0 flex-1">
      <p
        key="research-header-eyebrow"
        className="text-[11px] font-semibold uppercase text-[var(--vector-accent-ink)]"
        style={{ fontFamily: "var(--font-vector-display)" }}
      >
        THREAD
      </p>
      <h2
        key="research-header-title"
        className="truncate text-lg font-semibold text-[var(--vector-ink)]"
      >
        {thread.title}
      </h2>
    </div>
  );
  const headerActions = (
    <div
      key="research-header-actions"
      className="flex shrink-0 items-center gap-2"
    >
      <DeleteThreadButton
        key="research-delete-thread"
        threadId={thread.threadId}
        title={thread.title}
      />
      <ResearchLiveAnnouncer
        key="research-live-announcer"
        threadId={thread.threadId}
        activeRunId={announcementRunId}
        completedRunIds={completedIds}
      />
    </div>
  );
  const composer = (
    <ResearchComposer
      key="research-composer"
      threadId={thread.threadId}
      activeRunId={currentRunId}
    />
  );
  return (
    <section className="flex min-h-0 min-w-0 flex-1 flex-col bg-[var(--vector-surface-2)]">
      <ResearchLiveAnnouncementBoundary threadId={thread.threadId}>
        {withSourcesPanel ? (
          <ResearchSourcesPanel
            threadId={thread.threadId}
            messages={thread.messages}
            headerLeading={headerLeading}
            headerActions={headerActions}
            answerPanel={answerPanel}
            composer={composer}
          />
        ) : (
          <>
            <header className="flex items-center justify-between gap-3 border-b border-[var(--vector-rule)] bg-[var(--vector-surface)]/92 py-3 pr-4 pl-16">
              {headerLeading}
              {headerActions}
            </header>
            {answerPanel}
            {composer}
          </>
        )}
      </ResearchLiveAnnouncementBoundary>
    </section>
  );
}

export function ResearchEmptyView() {
  return (
    <section className="flex min-h-0 min-w-0 flex-1 flex-col bg-[var(--vector-surface-2)]">
      <header className="shrink-0 border-b border-[var(--vector-rule)] bg-[var(--vector-surface)]/92 py-3 pr-4 pl-16">
        <p
          className="text-[11px] font-semibold uppercase text-[var(--vector-accent-ink)]"
          style={{ fontFamily: "var(--font-vector-display)" }}
        >
          THREAD
        </p>
        <h2 className="truncate text-lg font-semibold text-[var(--vector-ink)]">
          新しいリサーチ
        </h2>
      </header>
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
