"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import { SystemStatus } from "@/components/system-status";
import { ResourceList } from "@/components/resource-list";

export default function Dashboard() {
  const router = useRouter();
  const [user, setUser] = useState<{ email: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    api<{ email: string }>("auth/me").then(value => { if (active) setUser(value); }).catch(error => {
      if (!active) return;
      if (error instanceof ApiError && error.status === 401) router.replace("/login");
      else setError(error instanceof Error ? error.message : "فشل الاتصال");
    });
    return () => { active = false; };
  }, [router]);
  async function logout() {
    setBusy(true); setError(null);
    try { await api("auth/logout", { method: "POST" }); router.replace("/login"); }
    catch (error) { setError(error instanceof Error ? error.message : "فشل الخروج"); }
    finally { setBusy(false); }
  }
  return <main className="mx-auto max-w-5xl space-y-6 px-6 py-12">
    <h1 className="text-2xl font-bold">لوحة التحكم</h1>
    {user && <ResourceList />}
    {error && <p role="alert" className="text-rose-300">{error}</p>}
    {!user && !error && <p>جارٍ التحقق من الجلسة…</p>}
    {user && <><p>{user.email}</p><button disabled={busy} onClick={logout} className="rounded bg-teal-700 px-5 py-2">تسجيل الخروج</button><SystemStatus /></>}
  </main>;
}