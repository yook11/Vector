"use server";

import { requireAdminForAction } from "@/lib/auth/guards";
import { isRedirectError } from "@/lib/utils/redirect-error";
import { ProvisionUserSchema } from "../schemas/auth";
import {
  CreateUserWithCredentialError,
  createUserWithCredential,
} from "../server/create-user-with-credential";

type ProvisionUserField = "name" | "email" | "password";

type ProvisionUserFieldErrors = Partial<Record<ProvisionUserField, string>>;

export type ProvisionUserState =
  | { status: "idle" }
  | {
      status: "error";
      formError: string;
      fieldErrors?: ProvisionUserFieldErrors;
    }
  | { status: "success"; email: string };

function validationErrorState(
  fieldErrors: Record<string, string[] | undefined>,
): ProvisionUserState {
  const errors: ProvisionUserFieldErrors = {};
  for (const field of ["name", "email", "password"] as const) {
    const message = fieldErrors[field]?.[0];
    if (message !== undefined) {
      errors[field] = message;
    }
  }

  return {
    status: "error",
    formError: "入力内容を確認してください。",
    ...(Object.keys(errors).length > 0 ? { fieldErrors: errors } : {}),
  };
}

function serviceErrorState(error: unknown): ProvisionUserState {
  return {
    status: "error",
    formError:
      error instanceof CreateUserWithCredentialError &&
      error.code === "duplicate-email"
        ? "このメールアドレスは登録済みです。"
        : "ユーザーの登録に失敗しました。",
  };
}

export async function provisionUser(
  _previousState: ProvisionUserState,
  formData: FormData,
): Promise<ProvisionUserState> {
  try {
    await requireAdminForAction();
  } catch (error) {
    if (isRedirectError(error)) {
      throw error;
    }
    return {
      status: "error",
      formError:
        error instanceof Error && error.message === "Forbidden"
          ? "この操作を行う権限がありません。"
          : "ユーザーの登録に失敗しました。",
    };
  }

  // Next Server Actionが自動付与する`$ACTION_` transport metadataだけをvalidation対象外にする。
  const payload = Object.fromEntries(
    Array.from(formData.entries()).filter(
      ([field]) => !field.startsWith("$ACTION_"),
    ),
  );
  const parsed = ProvisionUserSchema.safeParse(payload);
  if (!parsed.success) {
    return validationErrorState(parsed.error.flatten().fieldErrors);
  }

  try {
    await createUserWithCredential(parsed.data);
    return { status: "success", email: parsed.data.email };
  } catch (error) {
    return serviceErrorState(error);
  }
}
