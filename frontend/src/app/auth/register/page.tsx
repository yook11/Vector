import { RegisterForm } from "@/components/auth/RegisterForm";

export const metadata = {
  title: "Register - Vector",
};

export default function RegisterPage() {
  return (
    <main className="flex min-h-[calc(100vh-3.5rem)] items-center justify-center p-4">
      <RegisterForm />
    </main>
  );
}
