"use client";

import { ArrowDown } from "lucide-react";
import { type RefObject, useEffect, useRef, useState } from "react";

const AUTO_FOLLOW_DISTANCE_PX = 96;

interface ResearchLiveScrollButtonProps {
  containerRef: RefObject<HTMLElement | null>;
  contentRevision: number;
}

function distanceFromBottom(element: HTMLElement): number {
  return element.scrollHeight - element.scrollTop - element.clientHeight;
}

export function ResearchLiveScrollButton({
  containerRef,
  contentRevision,
}: ResearchLiveScrollButtonProps) {
  const [offersLatestAnswer, setOffersLatestAnswer] = useState(false);
  const lastDistance = useRef(0);
  const previousRevision = useRef(contentRevision);
  const measurementFrame = useRef<number | null>(null);
  const updateFrame = useRef<number | null>(null);
  const followOnUpdate = useRef(true);

  useEffect(() => {
    const container = containerRef.current;
    if (container === null) return;

    const measure = () => {
      lastDistance.current = distanceFromBottom(container);
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
    };
  }, [containerRef]);

  useEffect(() => {
    if (previousRevision.current === contentRevision) return;
    previousRevision.current = contentRevision;
    followOnUpdate.current = lastDistance.current <= AUTO_FOLLOW_DISTANCE_PX;
    if (updateFrame.current !== null) return;

    updateFrame.current = requestAnimationFrame(() => {
      updateFrame.current = null;
      const container = containerRef.current;
      if (container === null) return;
      if (followOnUpdate.current) {
        container.scrollTo({ top: container.scrollHeight, behavior: "auto" });
        lastDistance.current = 0;
        setOffersLatestAnswer(false);
        return;
      }
      setOffersLatestAnswer(true);
    });
  }, [containerRef, contentRevision]);

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
