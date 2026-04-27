import { LoginForm } from "@/features/auth/components/LoginForm";

export const metadata = {
  title: "Login - Vector",
};

export default function LoginPage() {
  return (
    <main className="flex min-h-[calc(100vh-3.5rem)] items-center justify-center p-4">
      <LoginForm />
    </main>
  );
}
