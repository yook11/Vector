import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { Suspense, use, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createRouterMock, type RouterMock } from "@/test/router-mock";

const mocks = vi.hoisted(() => ({
  router: undefined as RouterMock | undefined,
}));

vi.mock("next/navigation", () => ({ useRouter: () => mocks.router }));

import { ArticleListUpdateNotice } from "./ArticleListUpdateNotice";

function revisionResponse(revision: string) {
  return Promise.resolve(
    new Response(JSON.stringify({ revision }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

beforeEach(() => {
  mocks.router = createRouterMock();
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(() => revisionResponse("a")),
  );
  Object.defineProperty(document, "hidden", {
    configurable: true,
    value: false,
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("ArticleListUpdateNotice", () => {
  it("初回確認で識別子が同じなら案内を表示しない", async () => {
    render(<ArticleListUpdateNotice displayedRevision="a" />);

    await waitFor(() => expect(fetch).toHaveBeenCalledOnce());
    expect(fetch).toHaveBeenCalledWith("/api/news/revision", {
      cache: "no-store",
      signal: expect.any(AbortSignal),
    });
    expect(screen.queryByText("新しい記事が追加されました")).toBeNull();
  });

  it.each([
    "a",
    "b",
  ])("更新後の識別子が%sでも完了し、確認を再開する", async (nextRevision) => {
    vi.mocked(fetch).mockImplementation(() => revisionResponse("b"));
    let complete: (revision: string) => void = () => undefined;
    const refreshed = new Promise<string>((resolve) => {
      complete = resolve;
    });
    function RefreshScenario() {
      const [snapshot, setSnapshot] = useState<string | Promise<string>>("a");
      mocks.router?.refresh.mockImplementation(() => setSnapshot(refreshed));
      const revision = typeof snapshot === "string" ? snapshot : use(snapshot);
      return <ArticleListUpdateNotice displayedRevision={revision} />;
    }
    render(
      <Suspense fallback={<p>loading</p>}>
        <RefreshScenario />
      </Suspense>,
    );
    const button = await screen.findByRole("button", { name: "一覧を更新" });
    await act(async () => {
      button.click();
    });
    await act(async () => {
      button.click();
    });
    expect(mocks.router?.refresh).toHaveBeenCalledOnce();
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent("更新中…");
    vi.mocked(fetch).mockImplementation(() => revisionResponse(nextRevision));
    await act(async () => {
      complete(nextRevision);
    });
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("新しい記事が追加されました")).toBeNull();
    vi.mocked(fetch).mockImplementation(() => revisionResponse("c"));
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    expect(
      await screen.findByRole("button", { name: "一覧を更新" }),
    ).toBeEnabled();
  });

  it("確認失敗では一覧を維持し、表示復帰時に再試行する", async () => {
    vi.mocked(fetch)
      .mockRejectedValueOnce(new Error("offline"))
      .mockImplementationOnce(() => revisionResponse("b"));
    render(<ArticleListUpdateNotice displayedRevision="a" />);
    await waitFor(() => expect(fetch).toHaveBeenCalledOnce());
    expect(screen.queryByText("新しい記事が追加されました")).toBeNull();

    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: true,
    });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: false,
    });
    act(() => document.dispatchEvent(new Event("visibilitychange")));

    expect(await screen.findByText("新しい記事が追加されました")).toBeVisible();
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("非表示中は初回確認を開始しない", async () => {
    Object.defineProperty(document, "hidden", {
      configurable: true,
      value: true,
    });
    render(<ArticleListUpdateNotice displayedRevision="a" />);

    await act(() => Promise.resolve());
    expect(fetch).not.toHaveBeenCalled();
  });
});

function setHidden(hidden: boolean) {
  Object.defineProperty(document, "hidden", {
    configurable: true,
    value: hidden,
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

it("60秒間隔で確認し、非表示・検知後・破棄後には確認を止める", async () => {
  vi.useFakeTimers();
  const { unmount } = render(<ArticleListUpdateNotice displayedRevision="a" />);
  await act(() => vi.advanceTimersByTimeAsync(0));
  expect(fetch).toHaveBeenCalledTimes(1);
  await act(() => vi.advanceTimersByTimeAsync(59_999));
  expect(fetch).toHaveBeenCalledTimes(1);
  await act(() => vi.advanceTimersByTimeAsync(1));
  expect(fetch).toHaveBeenCalledTimes(2);
  await act(async () => setHidden(true));
  await act(() => vi.advanceTimersByTimeAsync(120_000));
  expect(fetch).toHaveBeenCalledTimes(2);
  vi.mocked(fetch).mockImplementation(() => revisionResponse("b"));
  await act(async () => setHidden(false));
  await act(() => vi.advanceTimersByTimeAsync(0));
  expect(screen.getByRole("button", { name: "一覧を更新" })).toBeEnabled();
  await act(() => vi.advanceTimersByTimeAsync(120_000));
  expect(fetch).toHaveBeenCalledTimes(3);
  unmount();
  await act(() => vi.advanceTimersByTimeAsync(120_000));
  expect(fetch).toHaveBeenCalledTimes(3);
});

it("タイムアウトした確認を中断し、60秒後に再試行する", async () => {
  vi.useFakeTimers();
  let signal: AbortSignal | undefined;
  vi.mocked(fetch).mockImplementationOnce(
    (_input, init) =>
      new Promise((_resolve, reject) => {
        signal = init?.signal ?? undefined;
        signal?.addEventListener("abort", () => reject(new Error("aborted")));
      }),
  );
  render(<ArticleListUpdateNotice displayedRevision="a" />);
  await act(() => vi.advanceTimersByTimeAsync(0));
  await act(() => vi.advanceTimersByTimeAsync(10_000));
  expect(signal?.aborted).toBe(true);
  expect(screen.queryByText("新しい記事が追加されました")).toBeNull();
  await act(() => vi.advanceTimersByTimeAsync(60_000));
  expect(fetch).toHaveBeenCalledTimes(2);
});

it("非表示前の古い応答が復帰後の確認結果を上書きしない", async () => {
  vi.useFakeTimers();
  let complete: (response: Response) => void = () => undefined;
  let signal: AbortSignal | undefined;
  vi.mocked(fetch).mockImplementationOnce((_input, init) => {
    signal = init?.signal ?? undefined;
    return new Promise((resolve) => {
      complete = resolve;
    });
  });
  render(<ArticleListUpdateNotice displayedRevision="a" />);
  await act(() => vi.advanceTimersByTimeAsync(0));
  await act(async () => setHidden(true));
  expect(signal?.aborted).toBe(true);
  await act(async () => setHidden(false));
  await act(() => vi.advanceTimersByTimeAsync(0));
  await act(async () => complete(await revisionResponse("old-request")));
  expect(screen.queryByText("新しい記事が追加されました")).toBeNull();
  await act(() => vi.advanceTimersByTimeAsync(60_000));
  expect(fetch).toHaveBeenCalledTimes(3);
});
