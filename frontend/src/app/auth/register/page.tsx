import { RegisterForm } from "@/features/auth";

export const metadata = {
  title: "Register - Vector",
};

export default function RegisterPage() {
  return (
    <main className="flex min-h-dvh items-center justify-center p-4">
      <RegisterForm />
    </main>
  );
}
