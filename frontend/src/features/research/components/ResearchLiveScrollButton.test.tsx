import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type ComponentProps, type RefObject, useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ResearchLiveScrollButton } from "./ResearchLiveScrollButton";

interface HarnessProps {
  contentRevision: number;
  stageRevision?: number;
  failedContractionRevision?: number;
}

type FailureAwareScrollProps = ComponentProps<
  typeof ResearchLiveScrollButton
> & {
  failedContractionRevision: number;
};

function Harness({
  contentRevision,
  stageRevision = 0,
  failedContractionRevision = 0,
}: HarnessProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const scrollProps: FailureAwareScrollProps = {
    containerRef: containerRef as RefObject<HTMLElement | null>,
    contentRevision,
    failedContractionRevision,
  };
  return (
    <>
      <div
        ref={containerRef}
        data-testid="scroll-container"
        data-stage-revision={stageRevision}
      >
        <div data-testid="failed-turn-anchor" data-research-answer-anchor />
      </div>
      <ResearchLiveScrollButton {...scrollProps} />
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

function configureMutableContainer(
  element: HTMLElement,
  geometry: {
    scrollHeight: number;
    clientHeight: number;
    scrollTop: number;
  },
) {
  let scrollHeight = geometry.scrollHeight;
  const clientHeight = geometry.clientHeight;
  let scrollTop = geometry.scrollTop;
  Object.defineProperties(element, {
    scrollHeight: {
      configurable: true,
      get: () => scrollHeight,
    },
    clientHeight: { configurable: true, value: clientHeight },
    scrollTop: {
      configurable: true,
      get: () => scrollTop,
      set: (value: number) => {
        scrollTop = Math.min(
          Math.max(0, value),
          Math.max(0, scrollHeight - clientHeight),
        );
      },
    },
  });
  const scrollTo = vi.fn((options: ScrollToOptions) => {
    element.scrollTop = Number(options.top ?? element.scrollTop);
  });
  Object.defineProperty(element, "scrollTo", {
    configurable: true,
    value: scrollTo,
  });
  return {
    scrollTo,
    setScrollHeight: (value: number) => {
      scrollHeight = value;
    },
  };
}

function configureFailedTurnAnchor(
  container: HTMLElement,
  initialDocumentTop: number,
) {
  let documentTop = initialDocumentTop;
  const anchor = screen.getByTestId("failed-turn-anchor");
  vi.spyOn(anchor, "getBoundingClientRect").mockImplementation(() => {
    const top = documentTop - container.scrollTop;
    return {
      x: 0,
      y: top,
      top,
      right: 0,
      bottom: top + 40,
      left: 0,
      width: 0,
      height: 40,
      toJSON: () => ({}),
    };
  });
  return {
    anchor,
    setDocumentTop: (value: number) => {
      documentTop = value;
    },
  };
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
  vi.restoreAllMocks();
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

  it("clamps the old scrollTop after a failed contraction observed at 97px", () => {
    const view = render(
      <Harness contentRevision={0} failedContractionRevision={0} />,
    );
    const container = screen.getByTestId("scroll-container");
    const geometry = configureMutableContainer(container, {
      scrollHeight: 1000,
      clientHeight: 500,
      scrollTop: 403,
    });
    configureFailedTurnAnchor(container, 704);
    act(flushAnimationFrames);
    requestAnimationFrameMock.mockClear();
    const oldScrollTop = container.scrollTop;

    geometry.setScrollHeight(850);
    view.rerender(
      <Harness contentRevision={0} failedContractionRevision={1} />,
    );
    expect(requestAnimationFrameMock).toHaveBeenCalledTimes(1);
    act(flushAnimationFrames);

    const target = Math.min(
      oldScrollTop,
      container.scrollHeight - container.clientHeight,
    );
    expect(Math.abs(container.scrollTop - target)).toBeLessThanOrEqual(1);

    requestAnimationFrameMock.mockClear();
    view.rerender(
      <Harness contentRevision={0} failedContractionRevision={1} />,
    );
    expect(requestAnimationFrameMock).not.toHaveBeenCalled();
    expect(Math.abs(container.scrollTop - target)).toBeLessThanOrEqual(1);
  });

  it("preserves the failed turn anchor after a contraction observed at 96px", () => {
    const view = render(
      <Harness contentRevision={0} failedContractionRevision={0} />,
    );
    const container = screen.getByTestId("scroll-container");
    const geometry = configureMutableContainer(container, {
      scrollHeight: 1000,
      clientHeight: 500,
      scrollTop: 404,
    });
    const configuredAnchor = configureFailedTurnAnchor(container, 704);
    act(flushAnimationFrames);
    requestAnimationFrameMock.mockClear();
    const anchorTopBeforeFailure =
      configuredAnchor.anchor.getBoundingClientRect().top;

    geometry.setScrollHeight(850);
    configuredAnchor.setDocumentTop(624);
    view.rerender(
      <Harness contentRevision={0} failedContractionRevision={1} />,
    );
    expect(requestAnimationFrameMock).toHaveBeenCalledTimes(1);
    act(flushAnimationFrames);

    expect(
      Math.abs(
        configuredAnchor.anchor.getBoundingClientRect().top -
          anchorTopBeforeFailure,
      ),
    ).toBeLessThanOrEqual(1);
    const scrollTopAfterFailure = container.scrollTop;

    requestAnimationFrameMock.mockClear();
    view.rerender(
      <Harness contentRevision={0} failedContractionRevision={1} />,
    );
    expect(requestAnimationFrameMock).not.toHaveBeenCalled();
    expect(container.scrollTop).toBe(scrollTopAfterFailure);
    expect(
      Math.abs(
        configuredAnchor.anchor.getBoundingClientRect().top -
          anchorTopBeforeFailure,
      ),
    ).toBeLessThanOrEqual(1);
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
