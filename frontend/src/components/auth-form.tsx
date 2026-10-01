"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";
import { api } from "@/lib/api";

export function AuthForm({ register = false }: { register?: boolean }) {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true); setError(null);
    try {
      await api(`auth/${register ? "register" : "login"}`, {
        method: "POST", body: JSON.stringify({ email: form.get("email"), password: form.get("password") }),
      });
      router.replace("/dashboard");
    } catch (error) {
      setError(error instanceof Error ? error.message : "فشل الاتصال");
    } finally { setBusy(false); }
  }
  return <main className="mx-auto max-w-md px-6 py-16">
    <h1 className="mb-8 text-2xl font-bold">{register ? "إنشاء حساب" : "تسجيل الدخول"}</h1>
    <form onSubmit={submit} className="space-y-5">
      <label className="block">البريد الإلكتروني<input name="email" type="email" required autoComplete="email"
        className="mt-2 w-full rounded border border-slate-600 p-3" dir="ltr" /></label>
      <label className="block">كلمة المرور<input name="password" type="password" required
        minLength={register ? 12 : 1} maxLength={128} autoComplete={register ? "new-password" : "current-password"}
        className="mt-2 w-full rounded border border-slate-600 p-3" dir="ltr" /></label>
      {error && <p role="alert" className="text-rose-300">{error}</p>}
      <button disabled={busy} className="rounded bg-teal-700 px-5 py-3">{busy ? "جارٍ الإرسال…" : register ? "إنشاء حساب" : "دخول"}</button>
    </form>
    <Link className="mt-6 block text-teal-300" href={register ? "/login" : "/register"}>{register ? "لديك حساب؟ تسجيل الدخول" : "إنشاء حساب جديد"}</Link>
    <Link className="mt-4 block" href="/">حالة النظام</Link>
  </main>;
}