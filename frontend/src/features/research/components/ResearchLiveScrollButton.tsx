"use client";

import { ArrowDown } from "lucide-react";
import { type RefObject, useEffect, useRef, useState } from "react";

const AUTO_FOLLOW_DISTANCE_PX = 96;

interface ResearchLiveScrollButtonProps {
  containerRef: RefObject<HTMLElement | null>;
  contentRevision: number;
  finalReplacementRevision?: number;
  failedContractionRevision?: number;
  isActive?: boolean;
}

function distanceFromBottom(element: HTMLElement): number {
  return element.scrollHeight - element.scrollTop - element.clientHeight;
}

function latestAnswerAnchor(container: HTMLElement): HTMLElement | null {
  const anchors = container.querySelectorAll<HTMLElement>(
    "[data-research-answer-anchor]",
  );
  return anchors.item(anchors.length - 1);
}

function latestFailedTurnAnchor(container: HTMLElement): HTMLElement | null {
  const anchors = container.querySelectorAll<HTMLElement>(
    "[data-research-turn-anchor]",
  );
  return anchors.item(anchors.length - 1) ?? latestAnswerAnchor(container);
}

function answerAnchorTop(container: HTMLElement): number | null {
  const anchor = latestAnswerAnchor(container);
  if (anchor === null) return null;
  return anchor.getBoundingClientRect().top;
}

function failedTurnAnchorTop(container: HTMLElement): number | null {
  const anchor = latestFailedTurnAnchor(container);
  if (anchor === null) return null;
  return anchor.getBoundingClientRect().top;
}

function answerAnchorIsOutsideViewport(container: HTMLElement): boolean {
  const anchor = latestAnswerAnchor(container);
  if (anchor === null) return true;
  const containerTop = container.getBoundingClientRect().top;
  const anchorRect = anchor.getBoundingClientRect();
  const relativeTop = anchorRect.top - containerTop;
  return (
    relativeTop > container.clientHeight || relativeTop + anchorRect.height < 0
  );
}

export function ResearchLiveScrollButton({
  containerRef,
  contentRevision,
  finalReplacementRevision = 0,
  failedContractionRevision = 0,
  isActive = true,
}: ResearchLiveScrollButtonProps) {
  const [offersLatestAnswer, setOffersLatestAnswer] = useState(false);
  const lastDistance = useRef(0);
  const previousRevision = useRef(contentRevision);
  const previousFinalReplacementRevision = useRef(finalReplacementRevision);
  const previousFailedContractionRevision = useRef(failedContractionRevision);
  const measurementFrame = useRef<number | null>(null);
  const updateFrame = useRef<number | null>(null);
  const finalReplacementFrame = useRef<number | null>(null);
  const failedContractionFrame = useRef<number | null>(null);
  const followOnUpdate = useRef(true);
  const hasUnseenUpdate = useRef(false);
  const lastScrollTop = useRef(0);
  const lastAnswerAnchorTop = useRef<number | null>(null);
  const lastFailedTurnAnchorTop = useRef<number | null>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (container === null) return;

    const measure = () => {
      lastDistance.current = distanceFromBottom(container);
      lastScrollTop.current = container.scrollTop;
      lastAnswerAnchorTop.current = answerAnchorTop(container);
      lastFailedTurnAnchorTop.current = failedTurnAnchorTop(container);
      if (lastDistance.current <= AUTO_FOLLOW_DISTANCE_PX) {
        setOffersLatestAnswer(false);
      }
    };

    const handleScroll = () => {
      measure();
      if (updateFrame.current !== null) {
        followOnUpdate.current =
          lastDistance.current <= AUTO_FOLLOW_DISTANCE_PX;
      }
    };
    container.addEventListener("scroll", handleScroll, { passive: true });
    measurementFrame.current = requestAnimationFrame(() => {
      measurementFrame.current = null;
      measure();
    });

    return () => {
      container.removeEventListener("scroll", handleScroll);
      if (measurementFrame.current !== null) {
        cancelAnimationFrame(measurementFrame.current);
      }
      if (updateFrame.current !== null) {
        cancelAnimationFrame(updateFrame.current);
      }
      if (finalReplacementFrame.current !== null) {
        cancelAnimationFrame(finalReplacementFrame.current);
      }
      if (failedContractionFrame.current !== null) {
        cancelAnimationFrame(failedContractionFrame.current);
      }
    };
  }, [containerRef]);

  useEffect(() => {
    if (previousRevision.current === contentRevision) return;
    previousRevision.current = contentRevision;
    if (!isActive) {
      hasUnseenUpdate.current = true;
      return;
    }
    followOnUpdate.current = lastDistance.current <= AUTO_FOLLOW_DISTANCE_PX;
    if (updateFrame.current !== null) return;

    updateFrame.current = requestAnimationFrame(() => {
      updateFrame.current = null;
      const container = containerRef.current;
      if (container === null) return;
      if (followOnUpdate.current) {
        container.scrollTo({ top: container.scrollHeight, behavior: "auto" });
        lastDistance.current = 0;
        lastScrollTop.current = container.scrollTop;
        lastAnswerAnchorTop.current = answerAnchorTop(container);
        lastFailedTurnAnchorTop.current = failedTurnAnchorTop(container);
        setOffersLatestAnswer(false);
        return;
      }
      lastScrollTop.current = container.scrollTop;
      lastAnswerAnchorTop.current = answerAnchorTop(container);
      lastFailedTurnAnchorTop.current = failedTurnAnchorTop(container);
      setOffersLatestAnswer(true);
    });
  }, [containerRef, contentRevision, isActive]);

  useEffect(() => {
    if (previousFinalReplacementRevision.current === finalReplacementRevision) {
      return;
    }
    previousFinalReplacementRevision.current = finalReplacementRevision;
    if (!isActive) {
      hasUnseenUpdate.current = true;
      return;
    }

    const distanceBeforeReplacement = lastDistance.current;
    const scrollTopBeforeReplacement = lastScrollTop.current;
    const anchorTopBeforeReplacement = lastAnswerAnchorTop.current;
    if (updateFrame.current !== null) {
      cancelAnimationFrame(updateFrame.current);
      updateFrame.current = null;
    }
    if (finalReplacementFrame.current !== null) return;

    finalReplacementFrame.current = requestAnimationFrame(() => {
      finalReplacementFrame.current = null;
      const container = containerRef.current;
      if (container === null) return;

      if (distanceBeforeReplacement > AUTO_FOLLOW_DISTANCE_PX) {
        container.scrollTop = scrollTopBeforeReplacement;
        setOffersLatestAnswer(answerAnchorIsOutsideViewport(container));
      } else if (anchorTopBeforeReplacement !== null) {
        const currentAnchorTop = answerAnchorTop(container);
        if (currentAnchorTop !== null) {
          container.scrollTop += currentAnchorTop - anchorTopBeforeReplacement;
        }
        setOffersLatestAnswer(false);
      }

      lastDistance.current = distanceFromBottom(container);
      lastScrollTop.current = container.scrollTop;
      lastAnswerAnchorTop.current = answerAnchorTop(container);
      lastFailedTurnAnchorTop.current = failedTurnAnchorTop(container);
    });
  }, [containerRef, finalReplacementRevision, isActive]);

  useEffect(() => {
    if (
      previousFailedContractionRevision.current === failedContractionRevision
    ) {
      return;
    }
    previousFailedContractionRevision.current = failedContractionRevision;
    if (!isActive) {
      hasUnseenUpdate.current = true;
      return;
    }

    const distanceBeforeFailure = lastDistance.current;
    const scrollTopBeforeFailure = lastScrollTop.current;
    const anchorTopBeforeFailure = lastFailedTurnAnchorTop.current;
    if (updateFrame.current !== null) {
      cancelAnimationFrame(updateFrame.current);
      updateFrame.current = null;
    }
    if (failedContractionFrame.current !== null) return;

    failedContractionFrame.current = requestAnimationFrame(() => {
      failedContractionFrame.current = null;
      const container = containerRef.current;
      if (container === null) return;

      if (distanceBeforeFailure > AUTO_FOLLOW_DISTANCE_PX) {
        const maxScrollTop = Math.max(
          0,
          container.scrollHeight - container.clientHeight,
        );
        container.scrollTop = Math.min(scrollTopBeforeFailure, maxScrollTop);
        setOffersLatestAnswer(answerAnchorIsOutsideViewport(container));
      } else if (anchorTopBeforeFailure !== null) {
        const currentAnchorTop = failedTurnAnchorTop(container);
        if (currentAnchorTop !== null) {
          container.scrollTop += currentAnchorTop - anchorTopBeforeFailure;
        }
        setOffersLatestAnswer(false);
      }

      lastDistance.current = distanceFromBottom(container);
      lastScrollTop.current = container.scrollTop;
      lastAnswerAnchorTop.current = answerAnchorTop(container);
      lastFailedTurnAnchorTop.current = failedTurnAnchorTop(container);
    });
  }, [containerRef, failedContractionRevision, isActive]);

  useEffect(() => {
    if (!isActive || !hasUnseenUpdate.current) return;
    hasUnseenUpdate.current = false;
    const container = containerRef.current;
    if (container === null) return;
    lastDistance.current = distanceFromBottom(container);
    setOffersLatestAnswer(lastDistance.current > AUTO_FOLLOW_DISTANCE_PX);
  }, [containerRef, isActive]);

  if (!offersLatestAnswer) return null;

  const scrollToLatest = () => {
    const container = containerRef.current;
    if (container === null) return;
    const reducedMotion = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    container.scrollTo({
      top: container.scrollHeight,
      behavior: reducedMotion ? "auto" : "smooth",
    });
    lastDistance.current = 0;
    lastScrollTop.current = container.scrollTop;
    lastAnswerAnchorTop.current = answerAnchorTop(container);
    lastFailedTurnAnchorTop.current = failedTurnAnchorTop(container);
    setOffersLatestAnswer(false);
  };

  return (
    <button
      type="button"
      onClick={scrollToLatest}
      className="absolute right-4 bottom-4 z-10 inline-flex items-center gap-1.5 rounded-full border border-[var(--vector-line)] bg-[var(--vector-paper)]/95 px-3 py-2 text-xs font-semibold text-[var(--vector-ink)] shadow-[0_8px_24px_rgba(34,28,22,0.12)] backdrop-blur-sm transition-colors hover:border-[var(--vector-accent)] hover:text-[var(--vector-accent-ink)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--vector-accent)]/35 motion-reduce:transition-none"
    >
      <ArrowDown aria-hidden="true" className="size-3.5" />
      最新の回答へ
    </button>
  );
}
