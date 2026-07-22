import { render, screen } from "@testing-library/react";
import type { ComponentType } from "react";
import { describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  provisionUser: vi.fn(),
}));

vi.mock("../api/provision-user", () => ({
  provisionUser: mocks.provisionUser,
}));

import * as authFeature from "../index";

function publicComponent(name: string): ComponentType {
  const component = Reflect.get(authFeature, name);
  expect(component).toEqual(expect.any(Function));
  if (typeof component !== "function") {
    throw new Error(
      `${name} は auth feature の Public API で公開する必要があります。`,
    );
  }
  return component as ComponentType;
}

describe("auth feature Public API", () => {
  it("ProvisionUserForm と ProvisionUserLink を公開する", () => {
    expect(Reflect.get(authFeature, "ProvisionUserForm")).toEqual(
      expect.any(Function),
    );
    expect(Reflect.get(authFeature, "ProvisionUserLink")).toEqual(
      expect.any(Function),
    );
  });

  it("ProvisionUserLink はデモユーザー登録画面への日本語導線を表示する", () => {
    const ProvisionUserLink = publicComponent("ProvisionUserLink");
    render(<ProvisionUserLink />);

    const link = screen.getByRole("link", { name: "デモユーザーを登録" });
    expect(link).toHaveAttribute("href", "/admin/users/new");
  });
});
