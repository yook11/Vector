import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type RefObject, useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ResearchLiveScrollButton } from "./ResearchLiveScrollButton";

interface HarnessProps {
  contentRevision: number;
  stageRevision?: number;
}

function Harness({ contentRevision, stageRevision = 0 }: HarnessProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  return (
    <>
      <div
        ref={containerRef}
        data-testid="scroll-container"
        data-stage-revision={stageRevision}
      />
      <ResearchLiveScrollButton
        containerRef={containerRef as RefObject<HTMLElement | null>}
        contentRevision={contentRevision}
      />
    </>
  );
}

let animationFrameId = 0;
let animationFrames = new Map<number, FrameRequestCallback>();
let requestAnimationFrameMock: ReturnType<typeof vi.fn>;

function flushAnimationFrames(): void {
  const pending = [...animationFrames.entries()];
  animationFrames = new Map();
  for (const [, callback] of pending) callback(performance.now());
}

function configureContainer(element: HTMLElement, distanceFromBottom: number) {
  Object.defineProperties(element, {
    scrollHeight: { configurable: true, value: 1000 },
    clientHeight: { configurable: true, value: 500 },
    scrollTop: {
      configurable: true,
      writable: true,
      value: 500 - distanceFromBottom,
    },
  });
  const scrollTo = vi.fn((options: ScrollToOptions) => {
    element.scrollTop = Number(options.top ?? element.scrollTop);
  });
  Object.defineProperty(element, "scrollTo", {
    configurable: true,
    value: scrollTo,
  });
  return scrollTo;
}

function setReducedMotion(matches: boolean): void {
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      matches,
      media: "(prefers-reduced-motion: reduce)",
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  );
}

beforeEach(() => {
  animationFrameId = 0;
  animationFrames = new Map();
  requestAnimationFrameMock = vi.fn((callback: FrameRequestCallback) => {
    animationFrameId += 1;
    animationFrames.set(animationFrameId, callback);
    return animationFrameId;
  });
  vi.stubGlobal("requestAnimationFrame", requestAnimationFrameMock);
  vi.stubGlobal(
    "cancelAnimationFrame",
    vi.fn((id: number) => animationFrames.delete(id)),
  );
  setReducedMotion(false);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ResearchLiveScrollButton", () => {
  it("auto-follows once when the pre-update distance is exactly 96px", () => {
    const view = render(<Harness contentRevision={0} />);
    const container = screen.getByTestId("scroll-container");
    configureContainer(container, 96);
    act(flushAnimationFrames);
    requestAnimationFrameMock.mockClear();

    view.rerender(<Harness contentRevision={1} />);
    expect(requestAnimationFrameMock).toHaveBeenCalledTimes(1);
    act(flushAnimationFrames);

    expect(container.scrollTop).toBe(1000);
    expect(
      screen.queryByRole("button", { name: "最新の回答へ" }),
    ).not.toBeInTheDocument();
  });

  it("keeps position at 97px and offers an explicit latest-answer button", () => {
    const view = render(<Harness contentRevision={0} />);
    const container = screen.getByTestId("scroll-container");
    configureContainer(container, 97);
    act(flushAnimationFrames);

    view.rerender(<Harness contentRevision={1} />);
    act(flushAnimationFrames);

    expect(container.scrollTop).toBe(403);
    expect(
      screen.getByRole("button", { name: "最新の回答へ" }),
    ).toBeInTheDocument();
  });

  it("coalesces burst content updates into one layout scroll", () => {
    const view = render(<Harness contentRevision={0} />);
    const container = screen.getByTestId("scroll-container");
    configureContainer(container, 20);
    act(flushAnimationFrames);
    requestAnimationFrameMock.mockClear();

    view.rerender(<Harness contentRevision={1} />);
    view.rerender(<Harness contentRevision={2} />);
    view.rerender(<Harness contentRevision={3} />);

    expect(requestAnimationFrameMock).toHaveBeenCalledTimes(1);
    act(flushAnimationFrames);
    expect(container.scrollTop).toBe(1000);
  });

  it("does not schedule content scrolling for a stage-only update", () => {
    const view = render(<Harness contentRevision={1} stageRevision={0} />);
    const container = screen.getByTestId("scroll-container");
    configureContainer(container, 97);
    act(flushAnimationFrames);
    requestAnimationFrameMock.mockClear();

    view.rerender(<Harness contentRevision={1} stageRevision={1} />);

    expect(requestAnimationFrameMock).not.toHaveBeenCalled();
    expect(
      screen.queryByRole("button", { name: "最新の回答へ" }),
    ).not.toBeInTheDocument();
  });

  it("resumes following with smooth scroll when the button is pressed", async () => {
    const user = userEvent.setup();
    const view = render(<Harness contentRevision={0} />);
    const container = screen.getByTestId("scroll-container");
    const scrollTo = configureContainer(container, 97);
    act(flushAnimationFrames);
    view.rerender(<Harness contentRevision={1} />);
    act(flushAnimationFrames);

    await user.click(screen.getByRole("button", { name: "最新の回答へ" }));

    expect(scrollTo).toHaveBeenLastCalledWith({
      top: 1000,
      behavior: "smooth",
    });
    expect(
      screen.queryByRole("button", { name: "最新の回答へ" }),
    ).not.toBeInTheDocument();
  });

  it("uses instant scroll when reduced motion is requested", async () => {
    setReducedMotion(true);
    const user = userEvent.setup();
    const view = render(<Harness contentRevision={0} />);
    const container = screen.getByTestId("scroll-container");
    const scrollTo = configureContainer(container, 97);
    act(flushAnimationFrames);
    view.rerender(<Harness contentRevision={1} />);
    act(flushAnimationFrames);

    await user.click(screen.getByRole("button", { name: "最新の回答へ" }));

    expect(scrollTo).toHaveBeenLastCalledWith({ top: 1000, behavior: "auto" });
  });

  it("does not move keyboard focus during content updates", () => {
    const focusTarget = document.createElement("button");
    document.body.append(focusTarget);
    focusTarget.focus();
    const view = render(<Harness contentRevision={0} />);
    const container = screen.getByTestId("scroll-container");
    configureContainer(container, 20);
    act(flushAnimationFrames);

    view.rerender(<Harness contentRevision={1} />);
    act(flushAnimationFrames);

    expect(document.activeElement).toBe(focusTarget);
    focusTarget.remove();
  });
});
