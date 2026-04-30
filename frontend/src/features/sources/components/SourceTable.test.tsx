import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Component, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { NewsSourceDetail } from "@/types";

const mocks = vi.hoisted(() => ({
  activateSource: vi.fn(),
  deactivateSource: vi.fn(),
  deleteSource: vi.fn(),
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}));

vi.mock("../api/activate-source", () => ({
  activateSource: mocks.activateSource,
}));

vi.mock("../api/deactivate-source", () => ({
  deactivateSource: mocks.deactivateSource,
}));

vi.mock("../api/delete-source", () => ({
  deleteSource: mocks.deleteSource,
}));

vi.mock("sonner", () => ({
  toast: {
    success: mocks.toastSuccess,
    error: vi.fn(),
  },
}));

vi.mock("@/lib/utils/toast-error", () => ({
  toastError: mocks.toastError,
}));

import { SourceTable } from "./SourceTable";

const SAMPLE_SOURCES: NewsSourceDetail[] = [
  {
    id: 1,
    name: "Hacker News",
    sourceType: "rss",
    siteUrl: "https://news.ycombinator.com",
    endpointUrl: "https://hnrss.org/frontpage",
    isActive: true,
    createdAt: "2026-01-01T00:00:00Z",
    updatedAt: "2026-01-01T00:00:00Z",
  },
  {
    id: 2,
    name: "TechCrunch",
    sourceType: "rss",
    siteUrl: "https://techcrunch.com",
    endpointUrl: "https://techcrunch.com/feed",
    isActive: false,
    createdAt: "2026-01-01T00:00:00Z",
    updatedAt: "2026-01-01T00:00:00Z",
  },
];

let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  vi.clearAllMocks();
  consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  consoleErrorSpy.mockRestore();
});

const switchFor = (name: string) =>
  screen.getByRole("switch", { name: new RegExp(name) });

const deleteTriggerFor = (rowName: string) => {
  // 各行の trigger は row 内に絞って取得 (AlertDialog 内の confirm と区別)
  const row = screen.getByRole("row", { name: new RegExp(rowName) });
  return within(row).getByRole("button", { name: "Delete" });
};

describe("SourceTable — 表示", () => {
  it("空配列で empty state 文言を表示する", () => {
    render(<SourceTable sources={[]} />);
    expect(screen.getByText("No sources configured")).toBeInTheDocument();
    expect(
      screen.getByText("Add a news source to start fetching articles."),
    ).toBeInTheDocument();
  });

  it("各 source の name / type / endpoint / Switch state を行表示する", () => {
    render(<SourceTable sources={SAMPLE_SOURCES} />);
    expect(screen.getByText("Hacker News")).toBeInTheDocument();
    expect(screen.getByText("TechCrunch")).toBeInTheDocument();
    // sourceType は upper case 表示
    expect(screen.getAllByText("RSS")).toHaveLength(2);
    expect(screen.getByText("https://hnrss.org/frontpage")).toBeInTheDocument();
    expect(switchFor("Hacker News")).toHaveAttribute("aria-checked", "true");
    expect(switchFor("TechCrunch")).toHaveAttribute("aria-checked", "false");
  });
});

describe("SourceTable — toggle (mid-flight)", () => {
  it("ON 状態の Switch click で deactivateSource を呼び optimistic に OFF へ flip", async () => {
    let resolveDeact!: (v: NewsSourceDetail) => void;
    mocks.deactivateSource.mockImplementation(
      () =>
        new Promise<NewsSourceDetail>((resolve) => {
          resolveDeact = resolve;
        }),
    );

    const user = userEvent.setup();
    render(<SourceTable sources={SAMPLE_SOURCES} />);

    await user.click(switchFor("Hacker News"));
    await waitFor(() => {
      expect(switchFor("Hacker News")).toHaveAttribute("aria-checked", "false");
    });
    expect(mocks.deactivateSource).toHaveBeenCalledWith(1);
    expect(mocks.activateSource).not.toHaveBeenCalled();

    // pending 中は他行も disabled (useTransition のグローバル pending)
    expect(switchFor("TechCrunch")).toBeDisabled();

    resolveDeact({ ...SAMPLE_SOURCES[0]!, isActive: false });
  });

  it("OFF 状態の Switch click で activateSource を呼び optimistic に ON へ flip", async () => {
    let resolveAct!: (v: NewsSourceDetail) => void;
    mocks.activateSource.mockImplementation(
      () =>
        new Promise<NewsSourceDetail>((resolve) => {
          resolveAct = resolve;
        }),
    );

    const user = userEvent.setup();
    render(<SourceTable sources={SAMPLE_SOURCES} />);

    await user.click(switchFor("TechCrunch"));
    await waitFor(() => {
      expect(switchFor("TechCrunch")).toHaveAttribute("aria-checked", "true");
    });
    expect(mocks.activateSource).toHaveBeenCalledWith(2);

    resolveAct({ ...SAMPLE_SOURCES[1]!, isActive: true });
  });
});

describe("SourceTable — toggle resolve / reject", () => {
  it("activate resolve で toast.success を name + enabled で呼ぶ", async () => {
    mocks.activateSource.mockResolvedValue({
      ...SAMPLE_SOURCES[1]!,
      isActive: true,
    });

    const user = userEvent.setup();
    render(<SourceTable sources={SAMPLE_SOURCES} />);
    await user.click(switchFor("TechCrunch"));

    await waitFor(() => {
      expect(mocks.toastSuccess).toHaveBeenCalledWith("TechCrunch enabled");
    });
    expect(mocks.toastError).not.toHaveBeenCalled();
  });

  it("deactivate resolve で toast.success を name + disabled で呼ぶ", async () => {
    mocks.deactivateSource.mockResolvedValue({
      ...SAMPLE_SOURCES[0]!,
      isActive: false,
    });

    const user = userEvent.setup();
    render(<SourceTable sources={SAMPLE_SOURCES} />);
    await user.click(switchFor("Hacker News"));

    await waitFor(() => {
      expect(mocks.toastSuccess).toHaveBeenCalledWith("Hacker News disabled");
    });
  });

  it("activate reject で toastError + Switch base state へ revert", async () => {
    const error = new Error("Forbidden");
    mocks.activateSource.mockRejectedValue(error);

    const user = userEvent.setup();
    render(<SourceTable sources={SAMPLE_SOURCES} />);
    await user.click(switchFor("TechCrunch"));

    await waitFor(() => {
      // startTransition 完了で optimistic が base (false) に revert
      expect(switchFor("TechCrunch")).toHaveAttribute("aria-checked", "false");
    });
    expect(mocks.toastError).toHaveBeenCalledWith(
      error,
      "ソースの更新に失敗しました",
    );
    expect(mocks.toastSuccess).not.toHaveBeenCalled();
  });
});

describe("SourceTable — delete (AlertDialog)", () => {
  it("Cancel ボタンで AlertDialog を閉じ deleteSource は呼ばない", async () => {
    const user = userEvent.setup();
    render(<SourceTable sources={SAMPLE_SOURCES} />);

    await user.click(deleteTriggerFor("Hacker News"));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(mocks.deleteSource).not.toHaveBeenCalled();
  });

  it("confirm Delete で deleteSource を呼び optimistic に行を削除する", async () => {
    let resolveDel!: () => void;
    mocks.deleteSource.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          resolveDel = resolve;
        }),
    );

    const user = userEvent.setup();
    render(<SourceTable sources={SAMPLE_SOURCES} />);

    await user.click(deleteTriggerFor("Hacker News"));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    // optimistic に行削除
    await waitFor(() => {
      expect(screen.queryByText("Hacker News")).not.toBeInTheDocument();
    });
    expect(mocks.deleteSource).toHaveBeenCalledWith(1);

    resolveDel();
  });

  it("delete resolve で toast.success", async () => {
    mocks.deleteSource.mockResolvedValue(undefined);

    const user = userEvent.setup();
    render(<SourceTable sources={SAMPLE_SOURCES} />);

    await user.click(deleteTriggerFor("TechCrunch"));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(mocks.toastSuccess).toHaveBeenCalledWith('Deleted "TechCrunch"');
    });
  });

  it("delete reject で toastError + 行が base 状態に revert で再表示", async () => {
    const error = new Error("Not Found");
    mocks.deleteSource.mockRejectedValue(error);

    const user = userEvent.setup();
    render(<SourceTable sources={SAMPLE_SOURCES} />);

    await user.click(deleteTriggerFor("Hacker News"));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(mocks.toastError).toHaveBeenCalledWith(
        error,
        "ソースの削除に失敗しました",
      );
    });
    // optimistic 削除は startTransition 完了で base に戻る → 行が再表示される
    expect(screen.getByText("Hacker News")).toBeInTheDocument();
  });
});

// 本物の Next.js 環境では redirect() throw を framework renderer が受けて
// navigation するが、jsdom test では受け手がない。ErrorBoundary を被せて
// React の transition error 経路で受け止め、(a) toastError 不発火
// (b) 再 throw が起きていること を positively 検証する。
class CaptureBoundary extends Component<
  { children: ReactNode; onCapture: (err: unknown) => void },
  { caught: boolean }
> {
  state = { caught: false };
  static getDerivedStateFromError() {
    return { caught: true };
  }
  componentDidCatch(err: unknown) {
    this.props.onCapture(err);
  }
  render() {
    return this.state.caught ? null : this.props.children;
  }
}

describe("SourceTable — redirect throw を握り潰さない", () => {
  const makeRedirectError = () =>
    Object.assign(new Error("NEXT_REDIRECT"), {
      digest: "NEXT_REDIRECT;replace;/auth/login?callbackUrl=%2F;307;",
    });

  it("activate が NEXT_REDIRECT を throw すると toastError / toast.success は呼ばれず redirect error が再 throw される", async () => {
    const redirectError = makeRedirectError();
    mocks.activateSource.mockRejectedValue(redirectError);

    const captured: unknown[] = [];
    const user = userEvent.setup();
    render(
      <CaptureBoundary onCapture={(e) => captured.push(e)}>
        <SourceTable sources={SAMPLE_SOURCES} />
      </CaptureBoundary>,
    );
    await user.click(switchFor("TechCrunch"));

    await waitFor(() => {
      expect(captured).toHaveLength(1);
    });
    expect(captured[0]).toBe(redirectError);
    expect(mocks.toastError).not.toHaveBeenCalled();
    expect(mocks.toastSuccess).not.toHaveBeenCalled();
  });

  it("delete が NEXT_REDIRECT を throw すると toastError は呼ばれず redirect error が再 throw される", async () => {
    const redirectError = makeRedirectError();
    mocks.deleteSource.mockRejectedValue(redirectError);

    const captured: unknown[] = [];
    const user = userEvent.setup();
    render(
      <CaptureBoundary onCapture={(e) => captured.push(e)}>
        <SourceTable sources={SAMPLE_SOURCES} />
      </CaptureBoundary>,
    );

    await user.click(deleteTriggerFor("Hacker News"));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => {
      expect(captured).toHaveLength(1);
    });
    expect(captured[0]).toBe(redirectError);
    expect(mocks.toastError).not.toHaveBeenCalled();
    expect(mocks.toastSuccess).not.toHaveBeenCalled();
  });
});

describe("SourceTable — pending 中の disabled", () => {
  it("toggle 実行中は同行・他行の Switch / Delete が disabled になる (連打防止)", async () => {
    let resolveDeact!: (v: NewsSourceDetail) => void;
    mocks.deactivateSource.mockImplementation(
      () =>
        new Promise<NewsSourceDetail>((resolve) => {
          resolveDeact = resolve;
        }),
    );

    const user = userEvent.setup();
    render(<SourceTable sources={SAMPLE_SOURCES} />);

    await user.click(switchFor("Hacker News"));
    await waitFor(() => {
      expect(switchFor("Hacker News")).toBeDisabled();
    });
    // 全行 disabled (useTransition pending は table 全体に効く)
    expect(switchFor("TechCrunch")).toBeDisabled();
    expect(deleteTriggerFor("Hacker News")).toBeDisabled();
    expect(deleteTriggerFor("TechCrunch")).toBeDisabled();

    resolveDeact({ ...SAMPLE_SOURCES[0]!, isActive: false });
  });
});
