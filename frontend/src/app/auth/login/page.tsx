import { LoginForm } from "@/features/auth";

export const metadata = {
  title: "Login - Vector",
};

export default function LoginPage() {
  return (
    <main className="flex min-h-dvh items-center justify-center p-4">
      <LoginForm />
    </main>
  );
}
