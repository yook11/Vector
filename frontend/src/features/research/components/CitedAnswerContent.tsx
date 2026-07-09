"use client";

import { Fragment } from "react";
import type { ResearchAssistantMessage } from "@/types/types.gen";
import { SourcePreviewBadge } from "./SourcePreviewBadge";

type ResearchCitationSource = ResearchAssistantMessage["sources"][number];

export type CitedAnswerSegment =
  | {
      type: "text";
      key: string;
      text: string;
    }
  | {
      type: "citation";
      key: string;
      source: ResearchCitationSource;
    };

const CITATION_MARKER_RE = /\[\[(\d+)\]\]/g;

export function parseCitedAnswerContent(
  content: string,
  sources: ResearchCitationSource[],
): CitedAnswerSegment[] {
  const sourcesByRef = new Map(
    sources.map((source) => [source.sourceRef, source] as const),
  );
  const segments: CitedAnswerSegment[] = [];
  let cursor = 0;

  for (const match of content.matchAll(CITATION_MARKER_RE)) {
    const marker = match[0];
    const ref = match[1];
    if (!ref) continue;
    const markerStart = match.index;
    if (markerStart > cursor) {
      segments.push({
        type: "text",
        key: `text-${cursor}-${markerStart}`,
        text: content.slice(cursor, markerStart),
      });
    }
    const source = sourcesByRef.get(ref);
    if (source) {
      segments.push({
        type: "citation",
        key: `citation-${markerStart}-${ref}`,
        source,
      });
    }
    cursor = markerStart + marker.length;
  }

  if (cursor < content.length) {
    segments.push({
      type: "text",
      key: `text-${cursor}-${content.length}`,
      text: content.slice(cursor),
    });
  }

  return segments;
}

interface CitedAnswerContentProps {
  content: string;
  sources: ResearchCitationSource[];
}

export function CitedAnswerContent({
  content,
  sources,
}: CitedAnswerContentProps) {
  const segments = parseCitedAnswerContent(content, sources);

  return (
    <>
      {segments.map((segment) =>
        segment.type === "citation" ? (
          <SourcePreviewBadge key={segment.key} source={segment.source} />
        ) : (
          <Fragment key={segment.key}>{segment.text}</Fragment>
        ),
      )}
    </>
  );
}
