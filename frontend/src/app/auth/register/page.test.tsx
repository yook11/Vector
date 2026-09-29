import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/features/auth", () => ({
  RegisterForm: () => <form aria-label="新規登録フォーム" />,
}));

import RegisterPage, { metadata } from "./page";

describe("RegisterPage", () => {
  it("公開登録フォームを表示し、タイトルを新規登録にする", () => {
    render(<RegisterPage />);

    expect(
      screen.getByRole("form", { name: "新規登録フォーム" }),
    ).toBeInTheDocument();
    expect(metadata.title).toBe("新規登録 - Vector");
  });
});
