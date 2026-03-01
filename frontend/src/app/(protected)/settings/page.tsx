import { redirect } from "next/navigation";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Settings | Vector",
};

export default async function SettingsPage() {
  redirect("/");
}
